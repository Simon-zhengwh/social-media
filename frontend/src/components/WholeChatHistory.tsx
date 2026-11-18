import { useEffect, useRef } from 'react'
import type { WholeImageEditState, WholeImageMessage, WholeImageVersion } from '../types'
import { imageModelLabel } from '../modelGeneration'
import { parseServerDate } from '../time'

type Props = {
  messages: WholeImageMessage[]
  versions: WholeImageVersion[]
  edit: WholeImageEditState
}

const messageTime = new Intl.DateTimeFormat('zh-CN', {
  month: 'numeric',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
})

function formatMessageTime(value: string): string {
  const date = parseServerDate(value)
  return Number.isNaN(date.getTime()) ? '' : messageTime.format(date)
}

function historicalRequestModels({ messages, versions, edit }: Props) {
  const versionsById = new Map(versions.map(version => [version.id, version]))
  const models = new Map<string, string>()
  let request: WholeImageMessage | undefined
  for (const message of messages) {
    if (message.role === 'user') {
      request = message
      continue
    }
    if (!request || request.image_model || message.role !== 'assistant' || !message.version_id) continue
    const version = versionsById.get(message.version_id)
    const model = version?.model_input?.model
    if (version?.operation === 'edit' && version.instruction === request.content && model) {
      models.set(request.id, model)
    }
  }
  if (request && !request.image_model && !models.has(request.id) &&
      edit.instruction === request.content && edit.image_model) {
    models.set(request.id, edit.image_model)
  }
  return models
}

function MessageItem({ message, model }: { message: WholeImageMessage; model?: string }) {
  const failed = message.status === 'failed'
  return (
    <li className={`chat-message chat-${message.role}${failed ? ' is-failed' : ''}`}>
      <div className="chat-bubble">
        <p>{message.content}</p>
        {message.attachments?.length || model ? (
          <div className="chat-message-details">
            {message.attachments?.length ? (
              <div className="chat-message-attachments">
                {message.attachments.map((attachment) => (
                  <img
                    key={attachment.asset_id}
                    src={attachment.thumb_url || attachment.url}
                    alt={attachment.name}
                    title={attachment.name}
                    loading="lazy"
                  />
                ))}
              </div>
            ) : null}
            {model ? <span className="chat-message-model">{imageModelLabel(model)}</span> : null}
          </div>
        ) : null}
        {message.version ? (
          <div className="chat-actions">
            <span className="chat-action-chip">v{message.version}</span>
          </div>
        ) : null}
      </div>
      <span className="chat-meta">
        {message.version ? `v${message.version} · ` : ''}
        {message.created_at ? `${formatMessageTime(message.created_at)} · ` : ''}
        {message.role === 'user' ? '你' : '设计模型'}
      </span>
    </li>
  )
}

export default function WholeChatHistory({ messages, versions, edit }: Props) {
  const listRef = useRef<HTMLUListElement | null>(null)
  const running = edit.status === 'running'
  const historicalModels = historicalRequestModels({ messages, versions, edit })

  useEffect(() => {
    const node = listRef.current
    if (node) {
      node.scrollTop = node.scrollHeight
    }
  }, [messages.length, running])

  return (
    <section className="studio-panel chat-history-panel" aria-label="修改记录">
      {messages.length > 3 ? (
        <div className="chat-head-actions chat-head-actions-bar">
          <button type="button" onClick={() => listRef.current?.scrollTo({ top: 0, behavior: 'smooth' })}>最早</button>
          <button type="button" onClick={() => listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: 'smooth' })}>最新</button>
        </div>
      ) : null}
      <ul className="chat-list" ref={listRef} aria-live="polite" aria-label="修改记录">
        {messages.map((message) => (
          <MessageItem key={message.id} message={message} model={message.image_model ?? historicalModels.get(message.id)} />
        ))}
        {running ? (
          <li className="chat-message chat-assistant">
            <div className="chat-bubble chat-typing" aria-label="正在处理">
              <span />
              <span />
              <span />
            </div>
          </li>
        ) : null}
      </ul>
      {!messages.length && !running ? (
        <p className="chat-history-empty">还没有修改记录。</p>
      ) : null}
    </section>
  )
}
