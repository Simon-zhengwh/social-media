import {
  useEffect,
  useRef,
  useState,
  type ClipboardEvent,
} from 'react'
import type { Asset, WholeImageEditState, WholeImageMessage } from '../types'
import { parseServerDate } from '../time'
import ImagePromptComposer from './ImagePromptComposer'
import AssetPicker from './AssetPicker'
import { imagePromptIssue, serializeImagePrompt, type PromptImage } from '../imageMentions'

type Props = {
  messages: WholeImageMessage[]
  edit: WholeImageEditState
  stage: string
  disabled: boolean
  onUpload: (files: File[]) => Promise<Asset[]>
  onSend: (instruction: string, attachments: Asset[]) => Promise<boolean>
  currentImage: PromptImage | null
  assets: Asset[]
  assetsLoading: boolean
  onLoadAssets: () => void
  onGoAssets: () => void
}

const MAX_ATTACHMENTS = 5

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

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '图片处理失败，请稍后重试'
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

export default function WholeChatPanel({
  messages,
  edit,
  stage,
  disabled,
  onUpload,
  onSend,
  currentImage,
  assets,
  assetsLoading,
  onLoadAssets,
  onGoAssets,
}: Props) {
  const [draft, setDraft] = useState('')
  const [attachments, setAttachments] = useState<Asset[]>([])
  const [uploading, setUploading] = useState(false)
  const [sending, setSending] = useState(false)
  const [attachmentError, setAttachmentError] = useState<string | null>(null)
  const listRef = useRef<HTMLUListElement | null>(null)
  const [pickerOpen, setPickerOpen] = useState(false)
  const promptImages: PromptImage[] = [...(currentImage ? [currentImage] : []),
    ...attachments.map(asset => ({ assetId: asset.id, name: asset.original_name, url: asset.url }))]
  const promptIssue = imagePromptIssue(draft, promptImages, 1000)
  const running = edit.status === 'running' || sending

  useEffect(() => {
    const node = listRef.current
    if (node) {
      node.scrollTop = node.scrollHeight
    }
  }, [messages.length, running])

  const addImages = async (files: File[]) => {
    if (disabled || running || uploading) {
      return
    }
    const images = files.filter((file) => file.type.startsWith('image/'))
    if (!images.length) {
      setAttachmentError('请选择图片文件')
      return
    }
    const remaining = MAX_ATTACHMENTS - attachments.length
    if (remaining <= 0) {
      setAttachmentError(`每轮最多附加 ${MAX_ATTACHMENTS} 张图片`)
      return
    }
    setUploading(true)
    setAttachmentError(null)
    try {
      const uploaded = await onUpload(images.slice(0, remaining))
      setAttachments((current) => [...new Map([...current, ...uploaded].map(asset => [asset.id, asset])).values()].slice(0, MAX_ATTACHMENTS))
      if (images.length > remaining) {
        setAttachmentError(`每轮最多附加 ${MAX_ATTACHMENTS} 张图片`)
      }
    } catch (error: unknown) {
      setAttachmentError(errorText(error))
    } finally {
      setUploading(false)
    }
  }

  const handlePaste = (event: ClipboardEvent<HTMLElement>) => {
    if (disabled || running || uploading) {
      return
    }
    const files = Array.from(event.clipboardData.items)
      .filter((item) => item.kind === 'file' && item.type.startsWith('image/'))
      .map((item) => item.getAsFile())
      .filter((file): file is File => file !== null)
    if (!files.length) {
      return
    }
    event.preventDefault()
    void addImages(files)
  }

  const send = async (value: string) => {
    const instruction = value.trim()
    if (!instruction || disabled || running || uploading) {
      return
    }
    const issue = imagePromptIssue(value, promptImages, 1000)
    if (issue) { setAttachmentError(issue); return }
    setSending(true)
    setAttachmentError(null)
    try {
      const accepted = await onSend(serializeImagePrompt(value, promptImages, 1000).trim(), attachments)
      if (accepted) {
        setDraft('')
        setAttachments([])
      }
    } catch (error: unknown) {
      setAttachmentError(errorText(error))
    } finally {
      setSending(false)
    }
  }

  return (
    <section className="studio-panel chat-panel" onPaste={handlePaste}>
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
              {sending ? '正在上传修改' : stage === 'editing' ? '正在处理' : '处理中'}
            </span>
          ) : null}
        </div>
      ) : null}

      <ul className="chat-list" ref={listRef} aria-live="polite" aria-label="完整对话记录">
        {messages.map((message) => <MessageItem key={message.id} message={message} />)}
        {running && !sending ? (
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
      {attachmentError ? (
        <div className="chat-error" role="alert">
          <strong>图片附件</strong>
          <span>{attachmentError}</span>
        </div>
      ) : null}

      <form
        className="chat-composer"
        onSubmit={(event) => {
          event.preventDefault()
          void send(draft)
        }}
      >
        <ImagePromptComposer
          value={draft} onChange={setDraft} images={promptImages} label="修改要求"
          placeholder="描述想改什么，输入 @ 选择图片" maxImages={MAX_ATTACHMENTS} addedCount={attachments.length} maxLength={1000}
          disabled={disabled || running} uploading={uploading} onSubmit={() => void send(draft)}
          onUploadImages={files => void addImages(files)}
          onRemoveImage={assetId => setAttachments(current => current.filter(asset => asset.id !== assetId))}
          onPickImages={() => { setPickerOpen(true); onLoadAssets() }}
          footerActions={
            <button type="submit" className="chat-send" disabled={disabled || running || uploading || !draft.trim() || Boolean(promptIssue)} aria-label="发送修改">
              <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3 10h12M10 5l5 5-5 5" /></svg>
            </button>
          }
        />
      </form>
      <AssetPicker key={pickerOpen ? 'chat-open' : 'chat-closed'} open={pickerOpen} purpose="product" title="选择改图参考图"
        assets={[...new Map([...attachments, ...assets].map(asset => [asset.id, asset])).values()]} loading={assetsLoading}
        multi selectedIds={attachments.map(asset => asset.id)} max={MAX_ATTACHMENTS}
        onClose={() => setPickerOpen(false)} onGoAssets={onGoAssets}
        onConfirm={selected => { setAttachments(selected); setPickerOpen(false) }} />
    </section>
  )
}
