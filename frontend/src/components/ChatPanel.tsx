import { useEffect, useRef, useState } from 'react'
import type { EditState, Message } from '../types'

type Props = {
  messages: Message[]
  edit: EditState
  stage: string
  disabled: boolean
  onSend: (instruction: string) => void
}

const QUICK_COMMANDS = [
  '标题改成周末限时特惠',
  '价格改成 169 元',
  '标题再大一点',
  'Logo 移到右上角',
  '背景换成夜晚餐厅',
  '重新排版，广告感强一点',
]

function MessageItem({ message }: { message: Message }) {
  const failed = message.status === 'failed'
  return (
    <li className={`chat-message chat-${message.role}${failed ? ' is-failed' : ''}`}>
      <div className="chat-bubble">
        <p>{message.content}</p>
        {message.actions && message.actions.length > 0 ? (
          <div className="chat-actions">
            {message.actions.map((action, index) => (
              <span key={`${message.id}-${index}`} className="chat-action-chip">
                {action.summary ?? action.type}
              </span>
            ))}
          </div>
        ) : null}
      </div>
      <span className="chat-meta">
        {message.version ? `v${message.version}` : ''}
        {message.version ? ' · ' : ''}
        {message.role === 'user' ? '你' : '设计 Agent'}
      </span>
    </li>
  )
}

export default function ChatPanel({ messages, edit, stage, disabled, onSend }: Props) {
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
      <div className="studio-panel-head">
        <div>
          <span className="eyebrow">EDIT AGENT</span>
          <h3>对话修改</h3>
        </div>
        {running ? (
          <span className="edit-running">
            <span className="spinner spinner-dark" aria-hidden="true" />
            {stage === 'regenerating_background'
              ? '重生成底图'
              : stage === 'integrating_logo'
                ? '融合 Logo'
                : '识别修改'}
          </span>
        ) : null}
      </div>

      <ul className="chat-list" ref={listRef} aria-live="polite">
        {messages.length === 0 ? (
          <li className="chat-empty">
            <strong>生成完成后，可以继续说想改什么</strong>
            <span>改字、改价格、调 Logo 都不会重画整张图。</span>
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

      <div className="quick-commands">
        {QUICK_COMMANDS.map((command) => (
          <button
            key={command}
            type="button"
            onClick={() => send(command)}
            disabled={disabled || running}
          >
            {command}
          </button>
        ))}
      </div>

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
          placeholder="例如：把主标题改成“周末双人套餐”，其他都不要动"
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
      <p className="chat-footnote">改文字只更新文字层；只有换背景指令才会调用生图。</p>
    </section>
  )
}
