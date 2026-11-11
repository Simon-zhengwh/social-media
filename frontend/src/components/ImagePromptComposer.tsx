import { useEffect, useId, useRef, useState, type ClipboardEvent, type KeyboardEvent, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { hydrateImagePrompt, imageMention, imagePromptIssue, parseImagePrompt, serializeImagePrompt, type PromptImage } from '../imageMentions'
import { ZoomableImage } from './ImageLightbox'

/** Kept in sync with the .image-reference-add-menu width in styles.css. */
const ADD_MENU_WIDTH = 144

function readEditor(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? ''
  if (!(node instanceof HTMLElement)) return ''
  if (node.dataset.imageMention !== undefined) return imageMention(node.dataset.imageMention)
  if (node.tagName === 'BR') return '\n'
  if (node.childNodes.length === 1 && node.firstChild instanceof HTMLBRElement) return ''
  let text = ''
  for (const child of node.childNodes) {
    if (child instanceof HTMLElement && /^(DIV|P)$/.test(child.tagName) && text && !text.endsWith('\n')) text += '\n'
    text += readEditor(child)
  }
  return text
}

function updateChip(node: HTMLElement, images: PromptImage[]) {
  const index = images.findIndex(image => image.assetId === node.dataset.imageMention)
  const image = images[index]
  node.className = `image-mention-chip${image ? '' : ' is-invalid'}`
  node.setAttribute('aria-label', image ? `图片${index + 1}：${image.name}` : '引用的图片已移除')
  node.title = image?.name ?? '请删除该引用，或重新添加原图片'
  const thumbnail = document.createElement('img')
  thumbnail.alt = ''
  if (image) thumbnail.src = image.url
  else thumbnail.hidden = true
  node.replaceChildren(thumbnail, document.createTextNode(image ? `图片${index + 1}` : '图片已移除'))
}

function mentionNode(assetId: string, images: PromptImage[]) {
  const node = document.createElement('span')
  node.dataset.imageMention = assetId
  node.contentEditable = 'false'
  node.draggable = false
  updateChip(node, images)
  return node
}

type EditorProps = {
  value: string
  images: PromptImage[]
  label: string
  placeholder: string
  disabled: boolean
  onChange: (value: string) => void
  onSubmit?: () => void
  onPasteImages?: (files: File[]) => void
}

export function ImageMentionEditor({ value, images, label, placeholder, disabled, onChange, onSubmit, onPasteImages }: EditorProps) {
  const editorRef = useRef<HTMLDivElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const caretRef = useRef<Range | null>(null)
  const boundCaretOffset = useRef<number | null>(null)
  const queryRange = useRef<Range | null>(null)
  const composing = useRef(false)
  const menuId = useId()
  const [query, setQuery] = useState<{ text: string; index: number; x: number; y: number; top: number; root: Element } | null>(null)

  const rememberCaret = () => {
    const selection = window.getSelection()
    if (selection?.rangeCount && editorRef.current?.contains(selection.anchorNode)) caretRef.current = selection.getRangeAt(0).cloneRange()
  }
  const detectQuery = () => {
    if (disabled || composing.current) return
    const selection = window.getSelection()
    const editor = editorRef.current
    if (!selection?.isCollapsed || !selection.rangeCount || !editor?.contains(selection.anchorNode) || document.activeElement !== editor) { setQuery(null); return }
    rememberCaret()
    const range = selection.getRangeAt(0)
    if (range.startContainer.nodeType !== Node.TEXT_NODE) { setQuery(null); return }
    const text = range.startContainer.textContent?.slice(0, range.startOffset) ?? ''
    const match = /@([^@\n\s{}]{0,40})$/.exec(text)
    if (!match) { setQuery(null); return }
    const replacement = range.cloneRange()
    replacement.setStart(range.startContainer, match.index)
    queryRange.current = replacement
    const rect = range.getBoundingClientRect()
    const fallback = editor.getBoundingClientRect()
    setQuery(previous => ({ text: match[1], index: previous?.text === match[1] ? previous.index : 0,
      x: rect.width || rect.height ? rect.left : fallback.left + 8,
      y: rect.height ? rect.bottom : fallback.top + 24, top: rect.height ? rect.top : fallback.top,
      root: editor.closest('dialog') ?? document.body }))
  }
  const emit = () => {
    const editor = editorRef.current
    if (editor) {
      const raw = readEditor(editor)
      const bound = composing.current ? raw : hydrateImagePrompt(raw, images)
      const selection = window.getSelection()
      if (raw !== bound && selection?.rangeCount && editor.contains(selection.anchorNode)) {
        const prefix = selection.getRangeAt(0).cloneRange()
        prefix.selectNodeContents(editor)
        prefix.setEnd(selection.anchorNode!, selection.anchorOffset)
        const wrapper = document.createElement('div')
        wrapper.append(prefix.cloneContents())
        boundCaretOffset.current = hydrateImagePrompt(readEditor(wrapper), images).length
      }
      onChange(bound)
    }
    rememberCaret()
    detectQuery()
  }

  useEffect(() => {
    const editor = editorRef.current
    if (!editor) return
    // An unavailable raw ordinal can become resolvable after adding images.
    // Bind it before later removals can make the same ordinal refer elsewhere.
    const bound = composing.current ? value : hydrateImagePrompt(value, images)
    if (bound !== value) { onChange(bound); return }
    if (readEditor(editor) !== value) {
      const fragment = document.createDocumentFragment()
      let remaining = boundCaretOffset.current
      let caret: { node: Node; offset: number | null } | null = null
      for (const part of parseImagePrompt(value)) {
        const node = part.type === 'image' ? mentionNode(part.assetId, images) : document.createTextNode(part.text)
        fragment.append(node)
        if (remaining !== null && !caret) {
          const length = part.type === 'image' ? imageMention(part.assetId).length : part.text.length
          if (remaining <= length) {
            caret = { node, offset: part.type === 'text' ? remaining : null }
          } else remaining -= length
        }
      }
      editor.replaceChildren(fragment)
      caretRef.current = null
      if (caret && document.activeElement === editor) {
        const range = document.createRange()
        if (caret.offset === null) range.setStartAfter(caret.node)
        else range.setStart(caret.node, caret.offset)
        range.collapse(true)
        const selection = window.getSelection()
        selection?.removeAllRanges()
        selection?.addRange(range)
        caretRef.current = range.cloneRange()
      }
      boundCaretOffset.current = null
    } else {
      editor.querySelectorAll<HTMLElement>('[data-image-mention]').forEach(node => updateChip(node, images))
    }
  }, [value, images, onChange])

  useEffect(() => {
    const outside = (event: PointerEvent) => {
      if (!editorRef.current?.contains(event.target as Node) && !menuRef.current?.contains(event.target as Node)) setQuery(null)
    }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [])

  const insert = (assetId: string, replaceQuery = true) => {
    const editor = editorRef.current
    if (!editor || disabled) return
    const stored = replaceQuery && query ? queryRange.current : caretRef.current
    const range = stored && editor.contains(stored.commonAncestorContainer) ? stored.cloneRange() : document.createRange()
    if (!stored || !editor.contains(range.commonAncestorContainer)) { range.selectNodeContents(editor); range.collapse(false) }
    range.deleteContents()
    const chip = mentionNode(assetId, images)
    const space = document.createTextNode(' ')
    const fragment = document.createDocumentFragment()
    fragment.append(chip, space)
    range.insertNode(fragment)
    range.setStartAfter(space)
    range.collapse(true)
    editor.focus({ preventScroll: true })
    const selection = window.getSelection()
    selection?.removeAllRanges()
    selection?.addRange(range)
    onChange(readEditor(editor))
    caretRef.current = range.cloneRange()
    setQuery(null)
  }

  const options = images.map((image, index) => ({ ...image, number: index + 1 }))
    .filter(image => !query?.text || `图片${image.number} ${image.name}`.toLowerCase().includes(query.text.toLowerCase()))
  const activeIndex = options.length ? (query?.index ?? 0) % options.length : 0
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.nativeEvent.isComposing || composing.current) return
    if (query) {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setQuery(null); return }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault()
        setQuery(previous => previous ? { ...previous, index: (activeIndex + (event.key === 'ArrowDown' ? 1 : options.length - 1)) % Math.max(1, options.length) } : null)
        return
      }
      if (event.key === 'Enter' || event.key === 'Tab') {
        event.preventDefault()
        event.stopPropagation()
        if (options[activeIndex]) insert(options[activeIndex].assetId)
        else setQuery(null)
        return
      }
    }
    if (event.key === 'Enter' && !event.shiftKey && onSubmit) { event.preventDefault(); onSubmit(); return }
    if (event.key === 'Backspace' || event.key === 'Delete') {
      const selection = window.getSelection()
      if (!selection?.isCollapsed || !selection.rangeCount) return
      const range = selection.getRangeAt(0)
      const node = range.startContainer
      const backwards = event.key === 'Backspace'
      let neighbour: Node | null = null
      if (node.nodeType === Node.TEXT_NODE && range.startOffset === (backwards ? 0 : node.textContent?.length)) neighbour = backwards ? node.previousSibling : node.nextSibling
      if (node === editorRef.current) neighbour = node.childNodes[range.startOffset + (backwards ? -1 : 0)] ?? null
      if (neighbour instanceof HTMLElement && neighbour.dataset.imageMention !== undefined) {
        event.preventDefault()
        neighbour.remove()
        emit()
      }
    }
  }
  const paste = (event: ClipboardEvent<HTMLDivElement>) => {
    if (disabled) return
    event.preventDefault()
    event.stopPropagation()
    const files = Array.from(event.clipboardData.files).filter(file => file.type.startsWith('image/'))
    if (files.length && onPasteImages) { onPasteImages(files); return }
    const text = event.clipboardData.getData('text/plain')
    const selection = window.getSelection()
    const editor = editorRef.current
    if (!editor || !selection?.rangeCount) return
    const range = selection.getRangeAt(0)
    if (!editor.contains(range.commonAncestorContainer)) return
    range.deleteContents()
    const pasted = document.createTextNode(text)
    range.insertNode(pasted)
    range.setStartAfter(pasted)
    range.collapse(true)
    selection.removeAllRanges()
    selection.addRange(range)
    emit()
  }
  const copy = (event: ClipboardEvent<HTMLDivElement>, cut = false) => {
    const selection = window.getSelection()
    if (!selection?.rangeCount || selection.isCollapsed) return
    const wrapper = document.createElement('div')
    wrapper.append(selection.getRangeAt(0).cloneContents())
    try {
      event.clipboardData.setData('text/plain', serializeImagePrompt(readEditor(wrapper), images))
      event.preventDefault()
      if (cut && !disabled) { selection.getRangeAt(0).deleteContents(); emit() }
    } catch { /* Keep the browser's copy fallback for invalid references. */ }
  }
  const menuHeight = Math.min(252, Math.max(68, options.length * 44 + 32))
  const menuWidth = Math.min(280, window.innerWidth - 16)
  const menuTop = query ? query.y + menuHeight + 8 <= window.innerHeight ? query.y + 6 : Math.max(8, query.top - menuHeight - 6) : 0

  return (
    <>
      <div ref={editorRef} className="image-mention-editor" role="textbox" aria-label={label} aria-multiline="true" aria-disabled={disabled}
        aria-autocomplete="list" aria-expanded={Boolean(query)} aria-controls={query ? menuId : undefined}
        aria-activedescendant={query && options.length ? `${menuId}-${activeIndex}` : undefined}
        contentEditable={!disabled} suppressContentEditableWarning data-placeholder={placeholder} data-empty={!value}
        onInput={emit} onKeyDown={onKeyDown} onKeyUp={event => { if (event.key !== 'Escape') detectQuery() }} onClick={detectQuery} onPaste={paste}
        onCopy={event => copy(event)} onCut={event => copy(event, true)}
        onDragOver={event => event.preventDefault()} onDrop={event => {
          event.preventDefault()
          event.stopPropagation()
          if (!disabled) {
            const files = Array.from(event.dataTransfer.files).filter(file => file.type.startsWith('image/'))
            if (files.length) onPasteImages?.(files)
          }
        }}
        onCompositionStart={() => { composing.current = true; setQuery(null) }}
        onCompositionEnd={() => { composing.current = false; emit() }}
        onBlur={() => { rememberCaret() }} />
      {query ? createPortal(
        <div ref={menuRef} id={menuId} className="image-mention-menu" role="listbox" aria-label="选择引用图片"
          style={{ position: 'fixed', left: Math.max(8, Math.min(query.x, window.innerWidth - menuWidth - 8)), top: menuTop, width: menuWidth }}>
          <div className="image-mention-menu-head">选择图片引用</div>
          {options.length ? options.map((image, index) => (
            <div key={image.assetId} id={`${menuId}-${index}`} role="option" aria-selected={index === activeIndex}
              className={index === activeIndex ? 'is-active' : ''} onMouseDown={event => event.preventDefault()}
              onMouseMove={() => setQuery(previous => previous ? { ...previous, index } : null)} onClick={() => insert(image.assetId)}>
              <img src={image.url} alt="" /><span><strong>图片{image.number}</strong><small>{image.name}</small></span>
            </div>
          )) : <p>没有匹配的图片，请先添加参考图。</p>}
        </div>, query.root,
      ) : null}
    </>
  )
}

type ComposerProps = EditorProps & {
  maxImages: number
  addedCount?: number
  maxLength?: number
  uploading?: boolean
  onRemoveImage?: (assetId: string) => void
  onUploadImages?: (files: File[]) => void
  onPickImages?: () => void
  footerActions?: ReactNode
}

export default function ImagePromptComposer({ images, maxImages, addedCount = images.length, maxLength = 5000,
  uploading = false, onRemoveImage, onUploadImages, onPickImages, footerActions, ...editorProps }: ComposerProps) {
  const [addOpen, setAddOpen] = useState(false)
  // The add tile sits at the start of the strip, so anchoring the menu to its right edge pushes it
  // out of the card and under the sidebar; open to the right unless that would leave the viewport.
  const [addMenuSide, setAddMenuSide] = useState<'left' | 'right'>('left')
  const addRef = useRef<HTMLDivElement>(null)
  const issue = addedCount > maxImages ? `最多使用 ${maxImages} 张参考图，请移除多余图片。` : imagePromptIssue(editorProps.value, images, maxLength)
  useEffect(() => {
    const close = (event: PointerEvent) => { if (!addRef.current?.contains(event.target as Node)) setAddOpen(false) }
    document.addEventListener('pointerdown', close)
    return () => document.removeEventListener('pointerdown', close)
  }, [])
  return (
    <div className={`image-prompt-composer${editorProps.disabled ? ' is-disabled' : ''}${issue ? ' has-error' : ''}`}>
      <div className="image-reference-strip">
        {images.map((image, index) => (
          <div className={`image-reference-tile${image.removable === false ? ' is-source' : ''}`} key={image.assetId}>
            <ZoomableImage className="image-reference-preview" src={image.url} alt={`图片${index + 1}：${image.name}`} title={image.name} />
            <span className="image-reference-label">图片{index + 1}{image.removable === false ? ' · 当前图' : ''}</span>
            {onRemoveImage && image.removable !== false ? <button type="button" className="image-reference-remove" aria-label={`移除参考图片${index + 1}`}
              disabled={editorProps.disabled || uploading} onClick={() => onRemoveImage(image.assetId)}>×</button> : null}
          </div>
        ))}
        {(onUploadImages || onPickImages) && addedCount < maxImages ? (
          <div className="image-reference-add-wrap" ref={addRef}>
            <button type="button" className="image-reference-add" aria-label="添加参考图" aria-expanded={addOpen}
              disabled={editorProps.disabled || uploading || addedCount >= maxImages} onClick={() => {
                if (!addOpen) {
                  const rect = addRef.current?.getBoundingClientRect()
                  if (rect) setAddMenuSide(rect.left + ADD_MENU_WIDTH + 8 <= window.innerWidth ? 'left' : 'right')
                }
                setAddOpen(previous => !previous)
              }}>
              <span aria-hidden="true">＋</span><small>{uploading ? '上传中' : '参考图'}<br />{addedCount}/{maxImages}</small>
            </button>
            {addOpen ? <div className={`image-reference-add-menu from-${addMenuSide}`}>
              {onUploadImages ? <label>上传图片<input type="file" multiple accept="image/*" aria-label="上传参考图"
                onChange={event => { const files = Array.from(event.target.files ?? []); if (files.length) onUploadImages(files); event.target.value = ''; setAddOpen(false) }} /></label> : null}
              {onPickImages ? <button type="button" onClick={() => { setAddOpen(false); onPickImages() }}>从资产库选择</button> : null}
            </div> : null}
          </div>
        ) : null}
      </div>
      <ImageMentionEditor {...editorProps} images={images} onPasteImages={onUploadImages ?? editorProps.onPasteImages} />
      {issue ? <p className="image-prompt-error" role="alert">{issue}</p> : null}
      <div className="image-prompt-footer"><span className="image-prompt-footnote">输入 @ 引用图片{addedCount >= maxImages ? ` · ${addedCount}/${maxImages}` : ''}</span>{footerActions}</div>
    </div>
  )
}
