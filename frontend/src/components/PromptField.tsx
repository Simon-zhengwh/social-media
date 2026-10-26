import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

import { optimizePrompt, type PromptOptimizePayload } from '../api'

type Props = {
  label: string
  availability: string
  placeholder: string
  prompt: string
  promptOptimized: boolean
  rows?: number
  busy: boolean
  canOptimize: boolean
  optimizationBlockedReason?: string
  optimizeContext: Omit<PromptOptimizePayload, 'prompt'>
  onPromptChange: (value: string, optimized?: boolean) => void
}

export default function PromptField({
  label,
  availability,
  placeholder,
  prompt,
  promptOptimized,
  rows = 5,
  busy,
  canOptimize,
  optimizationBlockedReason,
  optimizeContext,
  onPromptChange,
}: Props) {
  const [optimizing, setOptimizing] = useState(false)
  const [optimizeError, setOptimizeError] = useState<string | null>(null)
  const [draftPrompt, setDraftPrompt] = useState<string | null>(null)
  const previewDialogRef = useRef<HTMLDialogElement>(null)
  const previewButtonRef = useRef<HTMLButtonElement>(null)
  const previewOpen = draftPrompt !== null
  const contextKey = JSON.stringify(optimizeContext)
  const latestContextKey = useRef(contextKey)
  useEffect(() => {
    latestContextKey.current = contextKey
  }, [contextKey])
  useEffect(() => {
    if (!previewOpen) {
      return
    }
    const dialog = previewDialogRef.current
    const previewButton = previewButtonRef.current
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    dialog?.showModal()
    return () => {
      dialog?.close()
      document.body.style.overflow = previousOverflow
      previewButton?.focus()
    }
  }, [previewOpen])
  const disabled = busy || optimizing
  const optimizeDisabled = disabled || !canOptimize

  const handleChange = (value: string) => {
    setOptimizeError(null)
    onPromptChange(value)
  }

  const handleOptimize = async () => {
    if (optimizeDisabled) {
      return
    }
    setOptimizing(true)
    setOptimizeError(null)
    try {
      const result = await optimizePrompt({ ...optimizeContext, prompt })
      if (contextKey !== latestContextKey.current) {
        setOptimizeError('生成条件已变化，请重新优化提示词')
        return
      }
      onPromptChange(result.prompt, true)
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
      <textarea
        aria-label={label}
        value={prompt}
        maxLength={5000}
        rows={rows}
        placeholder={placeholder}
        onChange={(event) => handleChange(event.target.value)}
        disabled={disabled}
      />
      <div className="prompt-field-actions">
        <button
          type="button"
          className="mp-btn-secondary mp-btn-small"
          onClick={() => void handleOptimize()}
          disabled={optimizeDisabled}
          aria-label={optimizing ? '正在优化提示词' : undefined}
          title={
            canOptimize
              ? '根据当前风格、参考图和素材图优化提示词'
              : (optimizationBlockedReason ?? '请先完善当前生成条件')
          }
        >
          {optimizing ? <span className="spinner spinner-dark" aria-hidden="true" /> : null}
          {optimizing ? '优化中…' : '提示词优化'}
        </button>
        <button
          ref={previewButtonRef}
          type="button"
          className="mp-btn-secondary mp-btn-small"
          onClick={() => setDraftPrompt(prompt)}
          disabled={disabled}
        >
          预览
        </button>
      </div>

      {optimizeError ? (
        <span className="prompt-optimize-error" role="alert">
          {optimizeError}
        </span>
      ) : null}
      {draftPrompt !== null
        ? createPortal(
            <dialog
              ref={previewDialogRef}
              className="modal prompt-preview-dialog"
              aria-labelledby="prompt-preview-title"
              onCancel={() => setDraftPrompt(null)}
              onClose={() => setDraftPrompt(null)}
            >
              <div className="modal-head">
                <div>
                  <h2 id="prompt-preview-title">提示词预览</h2>
                  <p>可直接编辑，保存后用于后续生成。</p>
                </div>
                <button type="button" className="modal-close" onClick={() => setDraftPrompt(null)}>
                  关闭
                </button>
              </div>
              <textarea
                className="prompt-preview-textarea"
                aria-label="提示词内容"
                value={draftPrompt}
                maxLength={5000}
                onChange={(event) => setDraftPrompt(event.target.value)}
                disabled={disabled}
                autoFocus
              />
              <div className="prompt-preview-footer">
                <span>{draftPrompt.length} / 5000</span>
                <div className="prompt-preview-actions">
                  <button type="button" className="mp-btn-secondary" onClick={() => setDraftPrompt(null)}>
                    取消
                  </button>
                  <button
                    type="button"
                    className="mp-btn-primary"
                    disabled={disabled}
                    onClick={() => {
                      setOptimizeError(null)
                      onPromptChange(draftPrompt, promptOptimized)
                      setDraftPrompt(null)
                    }}
                  >
                    保存
                  </button>
                </div>
              </div>
            </dialog>,
            document.body,
          )
        : null}
    </div>
  )
}
