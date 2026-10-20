import { useState } from 'react'
import type { DesignState, Layer, LayerPatch, TextLayer, ImageLayer, VisualComponentLayer } from '../types'

type Props = {
  design: DesignState
  selectedId: string | null
  busy: boolean
  onSelect: (id: string | null) => void
  onPatch: (layerId: string, patch: LayerPatch) => void
  onRegenerateComponent?: (layerId: string) => void
}

const TYPE_LABELS: Record<Layer['type'], string> = {
  background: '底图',
  text: '文字',
  image: '图片',
  shape: '形状',
  visual_component: '组件',
}

const ROLE_LABELS: Record<string, string> = {
  headline: '主标题',
  subheadline: '副标题',
  price: '价格',
  offer: '促销',
  details: '活动信息',
  cta: '行动',
  brand: '品牌',
  legal: '说明',
}

const COMPONENT_LABELS: Record<string, string> = {
  headline: '笔刷标题',
  promotion_price: '价格丝带',
  offer_badge: '优惠贴片',
  activity_info: '活动组件',
  brand_lockup: '品牌组件',
}

function EyeIcon({ visible }: { visible: boolean }) {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <path d="M2.5 10s2.7-4.4 7.5-4.4 7.5 4.4 7.5 4.4-2.7 4.4-7.5 4.4S2.5 10 2.5 10Z" />
      <circle cx="10" cy="10" r="2.1" />
      {!visible ? <path d="M3.5 3.5l13 13" /> : null}
    </svg>
  )
}

function LockIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <rect x="4.5" y="8.5" width="11" height="7.5" rx="1.8" />
      <path d="M7 8.5V6.6a3 3 0 0 1 6 0v1.9" />
    </svg>
  )
}

function alignLabel(value: string) {
  if (value === 'center') return '居'
  if (value === 'right') return '右'
  return '左'
}

function TextControls({
  layer,
  busy,
  onPatch,
}: {
  layer: TextLayer
  busy: boolean
  onPatch: (patch: LayerPatch) => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(layer.content)

  const submit = () => {
    const next = draft.trim()
    if (next && next !== layer.content) {
      onPatch({ content: next })
    }
    setEditing(false)
  }

  const step = (delta: number) => {
    const size = Math.max(16, Math.min(300, Math.round(layer.style.font_size + delta)))
    if (size !== layer.style.font_size) {
      onPatch({ style: { font_size: size } })
    }
  }

  return (
    <div className="layer-controls" onClick={(event) => event.stopPropagation()}>
      {editing ? (
        <div className="layer-inline-edit">
          <input
            autoFocus
            value={draft}
            maxLength={500}
            aria-label="编辑文字内容"
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') submit()
              if (event.key === 'Escape') {
                setDraft(layer.content)
                setEditing(false)
              }
            }}
          />
          <button type="button" className="layer-mini-btn" onClick={submit} disabled={busy}>
            保存
          </button>
        </div>
      ) : (
        <button
          type="button"
          className="layer-edit-trigger"
          onClick={() => {
            setDraft(layer.content)
            setEditing(true)
          }}
          disabled={busy}
        >
          编辑文字
        </button>
      )}
      <div className="layer-style-row">
        <div className="layer-stepper" aria-label="字号">
          <button type="button" onClick={() => step(-4)} disabled={busy} aria-label="减小字号">
            −
          </button>
          <span className="tnum">{Math.round(layer.style.font_size)}</span>
          <button type="button" onClick={() => step(4)} disabled={busy} aria-label="增大字号">
            +
          </button>
        </div>
        <label className="layer-color" title="文字颜色">
          <input
            type="color"
            value={layer.style.color.slice(0, 7)}
            onChange={(event) => onPatch({ style: { color: event.target.value.toUpperCase() } })}
            disabled={busy}
          />
        </label>
        <div className="layer-aligns">
          {(['left', 'center', 'right'] as const).map((value) => (
            <button
              key={value}
              type="button"
              className={layer.style.align === value ? 'is-active' : ''}
              onClick={() => onPatch({ style: { align: value } })}
              disabled={busy}
              title={`${value} 对齐`}
            >
              {alignLabel(value)}
            </button>
          ))}
        </div>
        <div className="layer-rotate" aria-label="文字旋转">
          <button
            type="button"
            onClick={() =>
              onPatch({ rotation: Math.max(-45, (layer.rotation ?? 0) - 3) })
            }
            disabled={busy}
            title="逆时针旋转 3°"
          >
            ↺
          </button>
          <button
            type="button"
            onClick={() =>
              onPatch({ rotation: Math.min(45, (layer.rotation ?? 0) + 3) })
            }
            disabled={busy}
            title="顺时针旋转 3°"
          >
            ↻
          </button>
        </div>
      </div>
      <p className="layer-note">只改这一层，底图和其他图层不动。</p>
    </div>
  )
}

function ImageControls({
  layer,
  design,
  busy,
  onPatch,
}: {
  layer: ImageLayer
  design: DesignState
  busy: boolean
  onPatch: (patch: LayerPatch) => void
}) {
  const move = (anchor: 'top_left' | 'top_right' | 'bottom_left' | 'bottom_right') => {
    const margin = Math.round(design.canvas.width * 0.05)
    const x =
      anchor === 'top_left' || anchor === 'bottom_left'
        ? margin
        : design.canvas.width - margin - layer.size.width
    const y =
      anchor === 'top_left' || anchor === 'top_right'
        ? margin
        : design.canvas.height - margin - layer.size.height
    onPatch({ position: { x: Math.max(0, x), y: Math.max(0, y) } })
  }
  const scale = (factor: number) => {
    onPatch({
      size: {
        width: Math.round(layer.size.width * factor),
        height: Math.round(layer.size.height * factor),
      },
    })
  }
  return (
    <div className="layer-controls" onClick={(event) => event.stopPropagation()}>
      <div className="layer-style-row">
        <div className="layer-position-grid" aria-label="图片位置">
          <button type="button" onClick={() => move('top_left')} disabled={busy} title="左上角">
            ↖
          </button>
          <button type="button" onClick={() => move('top_right')} disabled={busy} title="右上角">
            ↗
          </button>
          <button type="button" onClick={() => move('bottom_left')} disabled={busy} title="左下角">
            ↙
          </button>
          <button type="button" onClick={() => move('bottom_right')} disabled={busy} title="右下角">
            ↘
          </button>
        </div>
        <div className="layer-stepper">
          <button type="button" onClick={() => scale(0.82)} disabled={busy} aria-label="缩小图片">
            −
          </button>
          <span className="tnum">{Math.round(layer.size.width)}</span>
          <button type="button" onClick={() => scale(1.2)} disabled={busy} aria-label="放大图片">
            +
          </button>
        </div>
      </div>
      <p className="layer-note">
        独立图片图层；可调整透明度和尺寸，不调用生图。
      </p>
    </div>
  )
}

function VisualControls({
  layer,
  busy,
  onPatch,
  onRegenerate,
}: {
  layer: VisualComponentLayer
  busy: boolean
  onPatch: (patch: LayerPatch) => void
  onRegenerate?: () => void
}) {
  const initialText = typeof layer.content.text === 'string' ? layer.content.text : ''
  const [draft, setDraft] = useState(initialText)
  const [editing, setEditing] = useState(false)
  const submit = () => {
    const next = draft.trim()
    if (next && next !== initialText) {
      onPatch({ content: { ...layer.content, text: next } })
    }
    setEditing(false)
  }
  const scale = (factor: number) => {
    onPatch({
      size: {
        width: Math.max(32, Math.round(layer.size.width * factor)),
        height: Math.max(32, Math.round(layer.size.height * factor)),
      },
    })
  }
  return (
    <div className="layer-controls" onClick={(event) => event.stopPropagation()}>
      {editing ? (
        <div className="layer-inline-edit">
          <input
            autoFocus
            value={draft}
            maxLength={500}
            aria-label="编辑组件文字"
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') submit()
              if (event.key === 'Escape') {
                setDraft(initialText)
                setEditing(false)
              }
            }}
          />
          <button type="button" className="layer-mini-btn" onClick={submit} disabled={busy}>
            保存
          </button>
        </div>
      ) : (
        <button
          type="button"
          className="layer-edit-trigger"
          onClick={() => {
            setDraft(initialText)
            setEditing(true)
          }}
          disabled={busy}
        >
          编辑组件文字
        </button>
      )}
      <div className="layer-style-row">
        <span className="layer-note">
          视觉素材 + 原生文字 · 融合 {layer.compositing?.blend_mode ?? 'normal'}
          {layer.compositing?.feather_px ? ` · ${layer.compositing.feather_px}px 边缘` : ''}
          {layer.compositing?.color_match ? ` · 色彩匹配 ${Math.round(layer.compositing.color_match * 100)}%` : ''}
        </span>
        <div className="layer-stepper">
          <button type="button" onClick={() => scale(0.9)} disabled={busy} aria-label="缩小组件">
            −
          </button>
          <span className="tnum">{Math.round(layer.size.width)}</span>
          <button type="button" onClick={() => scale(1.1)} disabled={busy} aria-label="放大组件">
            +
          </button>
        </div>
      </div>
      {onRegenerate ? (
        <button
          type="button"
          className="layer-regenerate-btn"
          onClick={onRegenerate}
          disabled={busy}
        >
          只重生成这个组件的笔刷素材
        </button>
      ) : null}
    </div>
  )
}

export default function LayerPanel({ design, selectedId, busy, onSelect, onPatch, onRegenerateComponent }: Props) {
  const layers = [...design.layers].sort((a, b) => b.z_index - a.z_index)
  return (
    <div className="layer-panel">
      <div className="layer-panel-head">
        <span>图层顺序</span>
        <span className="tnum">{layers.length} 层</span>
      </div>
      <ul className="layer-list">
        {layers.map((layer) => {
          const selected = selectedId === layer.id
          const textLayer = layer.type === 'text' ? (layer as TextLayer) : null
          return (
            <li key={layer.id}>
              <div
                className={`layer-row${selected ? ' is-selected' : ''}${layer.visible ? '' : ' is-hidden'}`}
                role="button"
                tabIndex={0}
                onClick={() => onSelect(selected ? null : layer.id)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    onSelect(selected ? null : layer.id)
                  }
                }}
              >
                <span className={`layer-type layer-type-${layer.type}`}>
                  {TYPE_LABELS[layer.type]}
                </span>
                <span className="layer-copy">
                  <strong>{layer.name}</strong>
                  <span className="layer-sub">
                    {layer.type === 'text'
                      ? `${ROLE_LABELS[textLayer?.role ?? ''] ?? textLayer?.role} · ${textLayer?.content}`
                      : layer.type === 'image'
                        ? '独立图片'
                        : layer.type === 'shape'
                          ? `${layer.shape} · 确定性形状`
                          : layer.type === 'visual_component'
                            ? `${COMPONENT_LABELS[layer.semantic_type] ?? layer.semantic_type} · ${String(layer.content.text ?? '')}`
                            : '锁定 · 独立版本'}
                  </span>
                </span>
                {layer.locked ? (
                  <span className="layer-lock" title="锁定图层">
                    <LockIcon />
                  </span>
                ) : (
                  <button
                    type="button"
                    className="layer-visibility"
                    title={layer.visible ? '隐藏图层' : '显示图层'}
                    onClick={(event) => {
                      event.stopPropagation()
                      onPatch(layer.id, { visible: !layer.visible })
                    }}
                    disabled={busy}
                  >
                    <EyeIcon visible={layer.visible} />
                  </button>
                )}
              </div>
              {selected ? (
                <>
                  {textLayer ? (
                    <TextControls
                      layer={textLayer}
                      busy={busy}
                      onPatch={(patch) => onPatch(layer.id, patch)}
                    />
                  ) : null}
                  {layer.type === 'image' ? (
                    <ImageControls
                      layer={layer as ImageLayer}
                      design={design}
                      busy={busy}
                      onPatch={(patch) => onPatch(layer.id, patch)}
                    />
                  ) : null}
                  {layer.type === 'visual_component' ? (
                    <VisualControls
                      layer={layer}
                      busy={busy}
                      onPatch={(patch) => onPatch(layer.id, patch)}
                      onRegenerate={
                        onRegenerateComponent ? () => onRegenerateComponent(layer.id) : undefined
                      }
                    />
                  ) : null}
                  {layer.type === 'background' ? (
                    <div className="layer-controls">
                      <p className="layer-note">
                        底图作为独立资产版本保存。在右侧对话里说“背景换成…”才会重生成底图。
                      </p>
                    </div>
                  ) : null}
                </>
              ) : null}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
