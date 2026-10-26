import { useEffect, useRef, useState } from 'react'
import type { WholeImageEditState, WholeImageMessage } from '../types'

type Props = {
  messages: WholeImageMessage[]
  edit: WholeImageEditState
  stage: string
  disabled: boolean
  onSend: (instruction: string) => void
}

const messageTime = new Intl.DateTimeFormat('zh-CN', {
  month: 'numeric',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
})

function formatMessageTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : messageTime.format(date)
}

function MessageItem({ message }: { message: WholeImageMessage }) {
  const failed = message.status === 'failed'
  return (
    <li className={`chat-message chat-${message.role}${failed ? ' is-failed' : ''}`}>
      <div className="chat-bubble">
        <p>{message.content}</p>
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

export default function WholeChatPanel({ messages, edit, stage, disabled, onSend }: Props) {
  const [draft, setDraft] = useState('')
  const listRef = useRef<HTMLUListElement | null>(null)
  const running = edit.status === 'running'

  useEffect(() => {
    const node = listRef.current
    if (node) {
      node.scrollTop = node.scrollHeight
    }
  }, [messages.length, running])

  const send = (value: string) => {
    const instruction = value.trim()
    if (!instruction || disabled || running) {
      return
    }
    onSend(instruction)
    setDraft('')
  }

  return (
    <section className="studio-panel chat-panel">
      {messages.length > 3 || running ? (
        <div className="chat-head-actions chat-head-actions-bar">
          {messages.length > 3 ? (
            <>
              <button
                type="button"
                onClick={() => listRef.current?.scrollTo({ top: 0, behavior: 'smooth' })}
              >
                最早
              </button>
              <button
                type="button"
                onClick={() => listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: 'smooth' })}
              >
                最新
              </button>
            </>
          ) : null}
          {running ? (
            <span className="edit-running">
              <span className="spinner spinner-dark" aria-hidden="true" />
              {stage === 'editing' ? '正在处理' : '处理中'}
            </span>
          ) : null}
        </div>
      ) : null}

      <ul className="chat-list" ref={listRef} aria-live="polite" aria-label="完整对话记录">
        {messages.length === 0 ? (
          <li className="chat-empty">
            <strong>生成完成后，可以继续说想改什么</strong>
            <span>每次修改都会带上首轮原始素材、当前版本和已确认修改，并保存为新的完整图片版本。</span>
          </li>
        ) : (
          messages.map((message) => <MessageItem key={message.id} message={message} />)
        )}
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

      {edit.status === 'failed' && edit.error ? (
        <div className="chat-error" role="alert">
          <strong>修改未执行</strong>
          <span>{edit.error}</span>
        </div>
      ) : null}

      <form
        className="chat-composer"
        onSubmit={(event) => {
          event.preventDefault()
          send(draft)
        }}
      >
        <textarea
          value={draft}
          rows={2}
          maxLength={1000}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              send(draft)
            }
          }}
          disabled={disabled || running}
        />
        <button
          type="submit"
          className="chat-send"
          disabled={disabled || running || !draft.trim()}
          aria-label="发送修改"
        >
          <svg viewBox="0 0 20 20" aria-hidden="true">
            <path d="M3 10h12M10 5l5 5-5 5" />
          </svg>
        </button>
      </form>

    </section>
  )
}
