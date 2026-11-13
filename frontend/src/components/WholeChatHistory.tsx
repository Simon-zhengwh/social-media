import { useEffect, useRef } from 'react'
import type { WholeImageEditState, WholeImageMessage } from '../types'
import { parseServerDate } from '../time'

type Props = {
  messages: WholeImageMessage[]
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

function MessageItem({ message }: { message: WholeImageMessage }) {
  const failed = message.status === 'failed'
  return (
    <li className={`chat-message chat-${message.role}${failed ? ' is-failed' : ''}`}>
      <div className="chat-bubble">
        <p>{message.content}</p>
        {message.attachments?.length ? (
          <div className="chat-message-attachments">
            {message.attachments.map((attachment) => (
              <img
                key={attachment.asset_id}
                src={attachment.url}
                alt={attachment.name}
                title={attachment.name}
                loading="lazy"
              />
            ))}
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

export default function WholeChatHistory({ messages, edit }: Props) {
  const listRef = useRef<HTMLUListElement | null>(null)
  const running = edit.status === 'running'

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
        {messages.map((message) => <MessageItem key={message.id} message={message} />)}
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
