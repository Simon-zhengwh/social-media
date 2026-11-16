export type EditorBinding = { sessionId: string; projectId: string; revision: number }
export type EditorSaveMessage = EditorBinding & { type: 'image-editor-v2:save'; project: Record<string, unknown> }

export function isEditorSaveMessage(value: unknown, binding: EditorBinding): value is EditorSaveMessage {
  if (!value || typeof value !== 'object') return false
  const data = value as Partial<EditorSaveMessage>
  const project = data.project
  return data.type === 'image-editor-v2:save' && data.sessionId === binding.sessionId &&
    data.projectId === binding.projectId && Number.isInteger(data.revision) && data.revision === binding.revision &&
    Boolean(project && project.schema === 'image-editor-v2' && project.assets && typeof project.assets === 'object' && Array.isArray(project.layers))
}

export function latestSaveQueue(persist: (project: Record<string, unknown>) => Promise<void>) {
  let writing = false
  let latest: Record<string, unknown> | undefined
  return async (project: Record<string, unknown>) => {
    latest = project
    if (writing) return
    writing = true
    try {
      while (latest) {
        const current = latest
        latest = undefined
        await persist(current)
      }
    } finally { latest = undefined; writing = false }
  }
}
