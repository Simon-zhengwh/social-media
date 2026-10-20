import { useEffect, useRef, useState } from 'react'
import type { WholeImageEditState, WholeImageMessage } from '../types'

type Props = {
  messages: WholeImageMessage[]
  edit: WholeImageEditState
  stage: string
  disabled: boolean
  onSend: (instruction: string) => void
}

const QUICK_COMMANDS = [
  '背景换成橙色，产品再突出一点',
  '整体换成更年轻大胆的风格',
  '标题改成「夏日限定」',
  '保留构图，只把文字改得更有冲击力',
]

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
      <div className="studio-panel-head">
        <div>
          <span className="eyebrow">WHOLE IMAGE EDIT</span>
          <h3>对话修改</h3>
        </div>
        {running ? (
          <span className="edit-running">
            <span className="spinner spinner-dark" aria-hidden="true" />
            {stage === 'editing' ? '原始素材 + 当前版本' : '处理中'}
          </span>
        ) : null}
      </div>

      <ul className="chat-list" ref={listRef} aria-live="polite">
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
          placeholder="例如：背景换成浅蓝色，其他都不要动"
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
      <p className="chat-footnote">
        每轮对话都会以首轮素材为约束、当前选中版本为视觉参考，并延续历史修改。
      </p>
    </section>
  )
}
