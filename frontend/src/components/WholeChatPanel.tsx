import {
  useState,
  type ClipboardEvent,
} from 'react'
import type { Asset, ImageModel, WholeImageEditState } from '../types'
import { DEFAULT_IMAGE_MODEL, IMAGE_MODEL_OPTIONS } from '../imageModels'
import ImagePromptComposer from './ImagePromptComposer'
import AssetPicker from './AssetPicker'
import { imagePromptIssue, serializeImagePrompt, type PromptImage } from '../imageMentions'

type Props = {
  initialModel: ImageModel
  edit: WholeImageEditState
  disabled: boolean
  onUpload: (files: File[]) => Promise<Asset[]>
  onSend: (instruction: string, attachments: Asset[], imageModel: ImageModel) => Promise<boolean>
  downloadHref: string | null
  currentImage: PromptImage | null
  assets: Asset[]
  assetsLoading: boolean
  onLoadAssets: () => void
  onGoAssets: () => void
}

const MAX_ATTACHMENTS = 5

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '图片处理失败，请稍后重试'
}

export default function WholeChatPanel({
  initialModel,
  edit,
  disabled,
  onUpload,
  onSend,
  downloadHref,
  currentImage,
  assets,
  assetsLoading,
  onLoadAssets,
  onGoAssets,
}: Props) {
  const [imageModel, setImageModel] = useState(() =>
    IMAGE_MODEL_OPTIONS.some(option => option.value === initialModel) ? initialModel : DEFAULT_IMAGE_MODEL)
  const [draft, setDraft] = useState('')
  const [attachments, setAttachments] = useState<Asset[]>([])
  const [uploading, setUploading] = useState(false)
  const [sending, setSending] = useState(false)
  const [attachmentError, setAttachmentError] = useState<string | null>(null)
  const [pickerOpen, setPickerOpen] = useState(false)
  const promptImages: PromptImage[] = [...(currentImage ? [currentImage] : []),
    ...attachments.map(asset => ({ assetId: asset.id, name: asset.original_name, url: asset.url }))]
  const promptIssue = imagePromptIssue(draft, promptImages, 1000)
  const running = edit.status === 'running' || sending

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
      const accepted = await onSend(serializeImagePrompt(value, promptImages, 1000).trim(), attachments, imageModel)
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
    <section className="studio-panel chat-panel" aria-label="对话改图" onPaste={handlePaste}>
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
            <div className="chat-composer-actions">
              {downloadHref ? (
                <a className="chat-download" href={downloadHref} download title="下载当前海报 PNG">
                  <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 3v9m0 0 3.5-3.5M10 12 6.5 8.5M4 15.5h12" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" /></svg>
                  下载 PNG
                </a>
              ) : null}
              <select className="chat-model-select" aria-label="改图模型" title="改图模型"
                value={imageModel} disabled={disabled || running || uploading}
                onChange={event => setImageModel(event.target.value as ImageModel)}>
                {IMAGE_MODEL_OPTIONS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
              <button type="submit" className="chat-send" disabled={disabled || running || uploading || !draft.trim() || Boolean(promptIssue)} aria-label="发送修改">
                <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3 10h12M10 5l5 5-5 5" /></svg>
              </button>
            </div>
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
