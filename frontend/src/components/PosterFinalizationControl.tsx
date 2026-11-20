import { useEffect, useState } from 'react'
import { ApiError, getPosterFinalization, revokePosterFinalization, savePosterFinalization } from '../api'
import type { PosterFinalization, PosterFinalizationContext, WholeImageTask } from '../types'
import ConfirmDialog from './ConfirmDialog'

type Props = {
  task: WholeImageTask
  disabled: boolean
  onChange?: (finalization: PosterFinalization | null, revokedGroupKey?: string) => void
}

export default function PosterFinalizationControl({ task, disabled, onChange }: Props) {
  const [context, setContext] = useState<PosterFinalizationContext | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [replacing, setReplacing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const versionId = task.current_version_id ?? ''
  const outputId = task.current_output_id ?? ''
  const final = context?.finalization
  const isCurrent = Boolean(final && final.task_id === task.id && final.version_id === versionId && final.output_id === outputId)
  const label = saving ? (isCurrent ? '撤销中…' : '定稿中…') : isCurrent ? '撤销定稿' : '定稿'

  useEffect(() => {
    let cancelled = false
    void getPosterFinalization(task.id, versionId, outputId).then(value => {
      if (!cancelled) {
        setContext(value)
        setError(null)
        onChange?.(value.finalization)
      }
    }).catch((reason: unknown) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : '定稿状态加载失败')
    }).finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [task.id, versionId, outputId, onChange])

  const save = async () => {
    if (loading || saving || isCurrent) return
    setSaving(true)
    setError(null)
    try {
      const current = context ?? await getPosterFinalization(task.id, versionId, outputId)
      const value = await savePosterFinalization(task.id, versionId, outputId, current.finalization?.revision ?? null)
      setContext({ ...current, finalization: value })
      onChange?.(value)
    } catch (reason: unknown) {
      if (reason instanceof ApiError && reason.status === 409) {
        const value = await getPosterFinalization(task.id, versionId, outputId).catch(() => null)
        setContext(value)
        if (value) onChange?.(value.finalization, value.finalization ? undefined : context?.finalization?.group_key)
      }
      setError(reason instanceof Error ? reason.message : '定稿保存失败')
    } finally { setSaving(false); setReplacing(false) }
  }

  const revoke = async () => {
    if (!final || saving) return
    setSaving(true)
    setError(null)
    try {
      await revokePosterFinalization(task.id, final.revision)
      setContext(context ? { ...context, finalization: null } : null)
      onChange?.(null, final.group_key)
    } catch (reason: unknown) {
      const value = await getPosterFinalization(task.id, versionId, outputId).catch(() => null)
      setContext(value)
      if (value) onChange?.(value.finalization, value.finalization ? undefined : final.group_key)
      setError(reason instanceof Error ? reason.message : '撤销定稿失败')
    } finally { setSaving(false) }
  }

  return <>
    <div className="poster-finalization-actions">
      <button type="button" className={`mp-btn-secondary mp-btn-small${isCurrent ? ' is-finalized' : ''}`}
        disabled={disabled || loading || saving} onClick={() => {
          if (isCurrent) void revoke()
          else if (final) setReplacing(true)
          else void save()
        }}>{label}</button>
      {error ? <span className="finalization-error" role="alert">{error}</span> : null}
    </div>
    {replacing ? <ConfirmDialog title="替换已有定稿？" description="这个任务已经有另一张定稿。将当前图片定稿，会撤销原定稿。是否继续？"
      confirmLabel="确认替换" tone="primary" busy={saving} busyLabel="替换中…"
      onConfirm={() => void save()} onCancel={() => setReplacing(false)} /> : null}
  </>
}
