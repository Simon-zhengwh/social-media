type RunTiming = {
  started_at?: string
  finished_at?: string
  remote_started_at?: string
  remote_completed_at?: string
}

export function duration(run: RunTiming, now: number) {
  const timestamp = (value?: string) => {
    const parsed = value ? Date.parse(value) : NaN
    return Number.isFinite(parsed) ? parsed : undefined
  }
  const start = timestamp(run.remote_started_at) ?? timestamp(run.started_at)
  if (start === undefined) return '等待开始'
  const end = timestamp(run.remote_completed_at) ?? timestamp(run.finished_at) ?? now
  const seconds = Math.max(0, Math.floor((end - start) / 1000))
  return `${Math.floor(seconds / 60)}分${String(seconds % 60).padStart(2, '0')}秒`
}
