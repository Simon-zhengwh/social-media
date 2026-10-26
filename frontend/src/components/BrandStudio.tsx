import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  ApiError,
  deleteBrand,
  deleteBrandLogo,
  deleteBrandReference,
  getBrand,
  updateBrand,
  uploadBrandLogos,
  uploadBrandReference,
} from '../api'
import type {
  BrandColor,
  BrandDetail,
  BrandStyle,
  PlatformPreset,
} from '../types'
import { ZoomableImage } from './ImageLightbox'

type Props = {
  brands: BrandStyle[]
  platforms: PlatformPreset[]
  brandsLoading: boolean
  brandsError: string | null
  onRefreshBrands: () => Promise<void>
}

type BrandDraft = {
  name: string
  short_name: string
  summary: string
  platform_id: string
  placement_id: string
  colors: BrandColor[]
  logo_asset_ids: string[]
  layout_guide: string
  background_guide: string
  logo_keywords: string[]
  note: string
  is_default: boolean
  cover_asset_id: string | null
}

function toDraft(detail: BrandDetail): BrandDraft {
  const layoutGuide = detail.layout_guide.trim()
  const backgroundGuide = detail.background_guide.trim()
  const generationGuide =
    !backgroundGuide || backgroundGuide === layoutGuide
      ? layoutGuide
      : `${layoutGuide}\n\n${backgroundGuide}`.trim()

  return {
    name: detail.name,
    short_name: detail.short_name,
    summary: detail.summary,
    platform_id: detail.platform_id,
    placement_id: detail.placement_id,
    colors: detail.colors.map((color) => ({ ...color })),
    logo_asset_ids: [...detail.logo_asset_ids],
    layout_guide: generationGuide,
    background_guide: generationGuide,
    logo_keywords: [...detail.logo_keywords],
    note: detail.note,
    is_default: detail.is_default,
    cover_asset_id: detail.cover_asset_id,
  }
}

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return '请求失败，请稍后重试'
}

function normalizeHex(value: string): string | null {
  const raw = value.trim().replace(/^#/, '')
  return /^[0-9a-fA-F]{6}$/.test(raw) ? `#${raw.toUpperCase()}` : null
}

function colorFromHex(hex: string): BrandColor {
  return { hex }
}

export default function BrandStudio({
  brands,
  platforms,
  brandsLoading,
  brandsError,
  onRefreshBrands,
}: Props) {
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<BrandDetail | null>(null)
  const [draft, setDraft] = useState<BrandDraft | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [uploadingLogo, setUploadingLogo] = useState(false)
  const [uploadingReference, setUploadingReference] = useState(false)
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<{ kind: 'logo' | 'reference'; id: string } | null>(null)
  const assetDeleteDialogRef = useRef<HTMLDialogElement>(null)
  const [coverBusyId, setCoverBusyId] = useState<string | null>(null)
  const [editingColor, setEditingColor] = useState<number | null>(null)
  const [colorInput, setColorInput] = useState('')
  const [brandDeleteId, setBrandDeleteId] = useState<string | null>(null)
  const [deletingBrand, setDeletingBrand] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    if (!confirmDelete) return
    const dialog = assetDeleteDialogRef.current
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    dialog?.showModal()
    return () => {
      dialog?.close()
      document.body.style.overflow = previousOverflow
      trigger?.focus()
    }
  }, [confirmDelete])

  const selectedPlatform = platforms.find((item) => item.id === draft?.platform_id)
  const logosFull = (detail?.logo_images.length ?? 0) >= 5
  const referencesFull = (detail?.reference_images.length ?? 0) >= 5

  const openBrand = async (brandId: string) => {
    setBrandDeleteId(null)
    setSelectedId(brandId)
    setLoading(true)
    setError(null)
    setNotice(null)
    setEditingColor(null)
    try {
      const data = await getBrand(brandId)
      setDetail(data)
      setDraft(toDraft(data))
    } catch (reason: unknown) {
      setError(errorText(reason))
    } finally {
      setLoading(false)
    }
  }

  const applyDetail = (data: BrandDetail, resetDraft = true) => {
    setDetail(data)
    setDraft((previous) =>
      resetDraft || !previous
        ? toDraft(data)
        : {
            ...previous,
            cover_asset_id: data.cover_asset_id,
            logo_asset_ids: [...data.logo_asset_ids],
          },
    )
    void onRefreshBrands()
  }

  const updateDraft = (patch: Partial<BrandDraft>) => {
    setDraft((previous) => (previous ? { ...previous, ...patch } : previous))
  }

  const payloadFromDraft = (source: BrandDraft) => ({
    ...source,
    name: source.name.trim(),
    short_name: source.name.trim(),
    summary: source.summary.trim(),
    platform: '',
    note: source.note,
    layout_guide: source.layout_guide.trim(),
    background_guide: source.background_guide.trim(),
  })

  const saveBrand = async () => {
    if (!selectedId || !draft) return
    setSaving(true)
    setError(null)
    setNotice(null)
    try {
      const data = await updateBrand(selectedId, payloadFromDraft(draft))
      applyDetail(data)
      setNotice('品牌配置已保存')
    } catch (reason: unknown) {
      setError(errorText(reason))
    } finally {
      setSaving(false)
    }
  }

  const removeBrand = async (brand: BrandStyle) => {
    setDeletingBrand(true)
    setError(null)
    setNotice(null)
    try {
      await deleteBrand(brand.id)
      setBrandDeleteId(null)
      setSelectedId(null)
      setDetail(null)
      setDraft(null)
      await onRefreshBrands()
      setNotice(`已删除品牌风格“${brand.name}”`)
    } catch (reason: unknown) {
      setError(errorText(reason))
    } finally {
      setDeletingBrand(false)
    }
  }
  const uploadLogos = async (files: File[]) => {
    if (!selectedId) return
    const accepted = files.slice(0, Math.max(0, 5 - (detail?.logo_images.length ?? 0)))
    if (!accepted.length) return
    setUploadingLogo(true)
    setError(null)
    setNotice(null)
    try {
      const data = await uploadBrandLogos(selectedId, accepted)
      applyDetail(data)
      setNotice(`已上传 ${data.logo_images.length - (detail?.logo_images.length ?? 0)} 张 Logo`)
    } catch (reason: unknown) {
      setError(errorText(reason))
    } finally {
      setUploadingLogo(false)
    }
  }

  const removeLogo = async (assetId: string) => {
    if (!selectedId) return
    setDeletingId(assetId)
    setConfirmDelete(null)
    setError(null)
    try {
      applyDetail(await deleteBrandLogo(selectedId, assetId))
      setNotice('Logo 已从品牌配置中移除')
    } catch (reason: unknown) {
      setError(errorText(reason))
    } finally {
      setDeletingId(null)
    }
  }

  const uploadReference = async (file: File) => {
    if (!selectedId) return
    setUploadingReference(true)
    setError(null)
    setNotice(null)
    try {
      applyDetail(await uploadBrandReference(selectedId, file), false)
      setNotice('参考图已上传')
    } catch (reason: unknown) {
      setError(errorText(reason))
    } finally {
      setUploadingReference(false)
    }
  }

  const removeReference = async (assetId: string) => {
    if (!selectedId) return
    setDeletingId(assetId)
    setConfirmDelete(null)
    setError(null)
    try {
      applyDetail(await deleteBrandReference(selectedId, assetId), false)
      setNotice('参考图已从品牌配置中移除')
    } catch (reason: unknown) {
      setError(errorText(reason))
    } finally {
      setDeletingId(null)
    }
  }

  const setCover = async (assetId: string) => {
    if (!selectedId || !draft) return
    setCoverBusyId(assetId)
    setError(null)
    try {
      applyDetail(
        await updateBrand(selectedId, {
          ...payloadFromDraft(draft),
          cover_asset_id: assetId,
        }),
        false,
      )
      setNotice('已设为品牌封面')
    } catch (reason: unknown) {
      setError(errorText(reason))
    } finally {
      setCoverBusyId(null)
    }
  }

  const updateColor = (index: number, hex: string) => {
    if (!draft) return
    setColorInput(hex)
    updateDraft({
      colors: draft.colors.map((color, current) =>
        current === index ? colorFromHex(hex) : color,
      ),
    })
  }

  const addColor = () => {
    if (!draft || draft.colors.length >= 6) return
    const fallback = ['#0D4317', '#B12310', '#FFF4DF', '#FFE36B', '#3A2318', '#FFFFFF']
    const hex = fallback.find((item) => !draft.colors.some((color) => color.hex === item)) ?? '#592BCE'
    updateDraft({ colors: [...draft.colors, colorFromHex(hex)] })
    setEditingColor(draft.colors.length)
    setColorInput(hex)
  }

  const moveColor = (index: number, offset: -1 | 1) => {
    if (!draft) return
    const target = index + offset
    if (target < 0 || target >= draft.colors.length) return
    const colors = [...draft.colors]
    ;[colors[index], colors[target]] = [colors[target], colors[index]]
    updateDraft({ colors })
    setEditingColor(target)
  }

  if (!selectedId) {
    return (
      <section className="asset-page brand-page">
        <div className="asset-head">
          <div>
            <span className="eyebrow">03 · 品牌库</span>
            <h1>品牌风格</h1>
            <p className="panel-lead">为同一商家维护不同发布平台、用途与品牌视觉。</p>
          </div>
          <div className="asset-stats">
            <span><strong className="tnum">{brands.length}</strong> 套风格</span>
          </div>
        </div>
        {brandsError ? <div className="asset-notice asset-notice-error">{brandsError}</div> : null}
        {error ? <div className="asset-notice asset-notice-error">{error}</div> : null}
        {notice ? <div className="asset-notice">{notice}</div> : null}
        {brandsLoading ? (
          <div className="brand-card-grid">
            {[0, 1, 2].map((item) => (
              <div className="brand-card brand-card-skeleton" key={item}>
                <span className="brand-card-cover" />
                <span className="asset-skeleton-line" />
                <span className="asset-skeleton-line asset-skeleton-short" />
              </div>
            ))}
          </div>
        ) : brands.length === 0 ? (
          <div className="asset-empty"><strong>还没有品牌配置</strong><span>请先导入品牌 skill。</span></div>
        ) : (
          <div className="brand-card-grid">
            {brands.map((brand) => (
              <article
                className="brand-card"
                key={brand.id}
                role="button"
                tabIndex={0}
                onClick={() => void openBrand(brand.id)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    void openBrand(brand.id)
                  }
                }}
              >
                <div className="brand-card-cover">
                  {brand.cover_url ? (
                    <img src={brand.cover_url} alt={`${brand.name} 风格封面`} />
                  ) : (
                    <span className="brand-cover-placeholder">{brand.short_name.slice(0, 2)}</span>
                  )}
                </div>
                <div className="brand-card-open">
                  <span className="brand-card-title">
                    <strong>{brand.name}</strong>
                    <span className="brand-platform-chip">{brand.platform || '未配置平台'}{brand.purpose ? ` · ${brand.purpose}` : ''}</span>
                  </span>
                  <span className="brand-card-summary">{brand.summary || '品牌专属视觉规范'}</span>
                </div>
                <div className="brand-card-footer">
                  <span className="brand-palette-mini">
                    {brand.colors.slice(0, 6).map((color) => <i key={color.hex} style={{ background: color.hex }} title={color.hex} />)}
                    {brand.logo_images[0] ? (
                      <img
                        className="brand-card-logo-static"
                        src={brand.logo_images[0].url}
                        alt={brand.logo_images[0].original_name || `${brand.name} Logo`}
                      />
                    ) : null}
                  </span>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>
    )
  }

  return (
    <section className="asset-page brand-page brand-detail-page">
      <div className="brand-detail-shell">
        <div className="brand-detail-head">
          <button type="button" className="brand-back" onClick={() => { setSelectedId(null); setDetail(null); setDraft(null) }}>
            ← 返回品牌列表
          </button>
          <div className="brand-detail-title">
            <div>
              <h1>{draft?.name || detail?.name || '品牌详情'}</h1>
            </div>

          </div>
        </div>

        {error ? <div className="asset-notice asset-notice-error">{error}</div> : null}
        {notice ? <div className="asset-notice">{notice}</div> : null}


        {loading || !draft ? (
          <div className="asset-empty"><strong>正在读取品牌配置</strong><span>请稍候…</span></div>
        ) : (
          <div className="brand-detail-body">
            <section className="brand-form-section brand-basics-section">
              <div className="brand-section-head">
                <div><span className="eyebrow">BASICS</span><h2>基本信息</h2></div>
              </div>

              <div className="brand-form-grid brand-basics-grid">
                <label className="brand-field brand-field-wide">
                  <span>品牌名称</span>
                  <input value={draft.name} onChange={(event) => updateDraft({ name: event.target.value })} />
                </label>

                <label className="brand-field">
                  <span>适用平台</span>
                  <select value={draft.platform_id} onChange={(event) => {
                    const next = platforms.find((item) => item.id === event.target.value)
                    updateDraft({ platform_id: event.target.value, placement_id: next?.default_placement_id ?? '' })
                  }}>
                    <option value="" disabled>请选择平台</option>
                    {platforms.map((platform) => <option key={platform.id} value={platform.id}>{platform.name}</option>)}
                  </select>
                </label>
                <label className="brand-field">
                  <span>发布用途</span>
                  <select value={draft.placement_id} disabled={!selectedPlatform} onChange={(event) => updateDraft({ placement_id: event.target.value })}>
                    {selectedPlatform ? selectedPlatform.placements.map((placement) => <option key={placement.id} value={placement.id}>{placement.name}</option>) : <option value="">请选择用途</option>}
                  </select>
                </label>
                <label className="brand-field brand-field-wide">
                  <span>品牌简介</span>
                  <textarea rows={2} value={draft.summary} onChange={(event) => updateDraft({ summary: event.target.value })} />
                </label>
              </div>

              <div className="brand-visual-assets">
                <div className="brand-asset-group">
                  <div className="brand-subhead"><strong>品牌 Logo</strong><span>选填 · {detail?.logo_images.length ?? 0}/5</span></div>
                  <div className="brand-logo-strip">
                    {detail?.logo_images.map((logo) => (
                      <div className="brand-logo-chip" key={logo.asset_id}>
                        <ZoomableImage
                          className="brand-logo-preview"
                          src={logo.url}
                          alt={logo.original_name}
                          title={logo.original_name}
                        />
                        <button type="button" className="brand-logo-remove" onClick={() => setConfirmDelete({ kind: 'logo', id: logo.asset_id })} aria-label="移除 Logo" disabled={deletingId === logo.asset_id}>×</button>
                      </div>
                    ))}
                    {!logosFull ? (
                      <label className="brand-asset-add">
                        <input type="file" multiple accept="image/png,image/jpeg,image/webp" disabled={uploadingLogo} onChange={(event) => { const files = Array.from(event.target.files ?? []); if (files.length) void uploadLogos(files); event.target.value = '' }} />
                        <span>＋</span><small>{uploadingLogo ? '上传中' : 'Logo'}</small>
                      </label>
                    ) : null}
                  </div>
                </div>

                <div className="brand-asset-group">
                  <div className="brand-subhead"><strong>主色调</strong><span>选填 · {draft.colors.length}/6</span></div>
                  <div className="brand-color-strip">
                    {draft.colors.map((color, index) => (
                      <button type="button" className={`brand-color-swatch${editingColor === index ? ' is-active' : ''}`} key={`${color.hex}-${index}`} style={{ background: color.hex }} title={color.hex} aria-label={color.hex} onClick={() => { setEditingColor(index); setColorInput(color.hex) }} />
                    ))}
                    {draft.colors.length < 6 ? <button type="button" className="brand-color-add" onClick={addColor}>＋</button> : null}
                  </div>
                  {editingColor !== null && draft.colors[editingColor] ? (
                    <div className="brand-color-editor">
                      <input type="color" value={draft.colors[editingColor].hex} onChange={(event) => updateColor(editingColor, event.target.value.toUpperCase())} aria-label="选择颜色" />
                      <input value={colorInput} maxLength={7} onChange={(event) => {
                        const value = event.target.value.startsWith('#') ? event.target.value : `#${event.target.value}`
                        setColorInput(value)
                        const normalized = normalizeHex(value)
                        if (normalized) updateDraft({ colors: draft.colors.map((color, current) => current === editingColor ? colorFromHex(normalized) : color) })
                      }} onBlur={() => setColorInput(draft.colors[editingColor].hex)} />
                      <code>{draft.colors[editingColor].hex}</code>
                      <button type="button" onClick={() => moveColor(editingColor, -1)} disabled={editingColor === 0}>←</button>
                      <button type="button" onClick={() => moveColor(editingColor, 1)} disabled={editingColor === draft.colors.length - 1}>→</button>
                      <button type="button" className="brand-color-remove" onClick={() => { updateDraft({ colors: draft.colors.filter((_, current) => current !== editingColor) }); setEditingColor(null) }}>删除</button>
                    </div>
                  ) : <span className="brand-color-hint">悬停查看 HEX，点击色块编辑。</span>}
                </div>
              </div>
            </section>

            <section className="brand-form-section">
              <div className="brand-section-head">
                <div><span className="eyebrow">GUIDES</span><h2>生成规范</h2></div>
              </div>
              <label className="brand-field">
                <textarea
                  className="brand-guide-input"
                  rows={10}
                  aria-label="生成规范"
                  value={draft.layout_guide}
                  onChange={(event) => {
                    const value = event.target.value
                    updateDraft({ layout_guide: value, background_guide: value })
                  }}
                />
              </label>
            </section>

            <section className="brand-form-section">
              <div className="brand-section-head">
                <div><span className="eyebrow">REFERENCES</span><h2>风格参考图</h2></div>
                <span className="brand-section-hint">最多 5 张 · {detail?.reference_images.length ?? 0}/5</span>
              </div>
              <div className="brand-reference-grid">
                {detail?.reference_images.map((reference) => (
                  <article className="brand-reference-card" key={reference.asset_id}>
                    <ZoomableImage
                      className="brand-reference-image"
                      src={reference.url}
                      alt={reference.original_name}
                      title={reference.original_name}
                    />
                    <div className="brand-reference-meta"><strong>{reference.original_name}</strong><span>{reference.width} × {reference.height}</span></div>
                    <div className="brand-reference-actions">
                      {reference.is_cover ? <span className="brand-cover-badge">当前封面</span> : <button type="button" className="mp-btn-ghost mp-btn-small" onClick={() => void setCover(reference.asset_id)} disabled={coverBusyId !== null}>{coverBusyId === reference.asset_id ? '设置中…' : '设为封面'}</button>}
                      <button type="button" className="brand-reference-delete" onClick={() => setConfirmDelete({ kind: 'reference', id: reference.asset_id })} disabled={deletingId === reference.asset_id}>删除</button>
                    </div>
                  </article>
                ))}
                {!referencesFull ? (
                  <label className={`brand-reference-upload${uploadingReference ? ' is-disabled' : ''}`}>
                    <input type="file" accept="image/png,image/jpeg,image/webp" disabled={uploadingReference} onChange={(event) => { const file = event.target.files?.[0]; if (file) void uploadReference(file); event.target.value = '' }} />
                    <span className="upload-plus" aria-hidden="true" /><strong>{uploadingReference ? '上传中…' : '上传参考图'}</strong><span>PNG / JPG / WebP</span>
                  </label>
                ) : null}
              </div>
            </section>

            {confirmDelete
              ? createPortal(
                  <dialog
                    ref={assetDeleteDialogRef}
                    className="brand-delete-modal brand-asset-delete-modal"
                    aria-labelledby="brand-asset-delete-title"
                    onCancel={() => setConfirmDelete(null)}
                    onClose={() => setConfirmDelete(null)}
                  >
                    <strong id="brand-asset-delete-title">
                      移除{confirmDelete.kind === 'logo' ? '品牌 Logo' : '参考图'}？
                    </strong>
                    <p>确认从当前风格中移除？素材文件和历史任务会保留。</p>
                    <div className="brand-delete-modal-actions">
                      <button type="button" onClick={() => setConfirmDelete(null)} autoFocus>取消</button>
                      <button type="button" className="is-danger" onClick={() => {
                        if (confirmDelete.kind === 'logo') void removeLogo(confirmDelete.id)
                        else void removeReference(confirmDelete.id)
                      }}>确认移除</button>
                    </div>
                  </dialog>,
                  document.body,
                )
              : null}

            <div className="brand-detail-footer">
              <div className="brand-detail-footer-actions">
                <button
                  type="button"
                  className="brand-detail-delete"
                  onClick={() => { if (selectedId) setBrandDeleteId(selectedId) }}
                  disabled={!detail || deletingBrand}
                >
                  删除品牌
                </button>
                <button type="button" className="mp-btn-primary" onClick={() => void saveBrand()} disabled={saving}>{saving ? '保存中…' : '保存品牌配置'}</button>
              </div>
            </div>
          </div>
        )}

        {brandDeleteId === selectedId ? (
          <div
            className="brand-delete-modal-backdrop"
            onMouseDown={(event) => {
              if (event.target === event.currentTarget && !deletingBrand) {
                setBrandDeleteId(null)
              }
            }}
          >
            <div
              className="brand-delete-modal"
              role="dialog"
              aria-modal="true"
              aria-labelledby="brand-delete-modal-title"
              onMouseDown={(event) => event.stopPropagation()}
              onKeyDown={(event) => {
                if (event.key === 'Escape' && !deletingBrand) {
                  setBrandDeleteId(null)
                }
              }}
            >
              <strong id="brand-delete-modal-title">删除品牌风格？</strong>
              <p>“{draft?.name || detail?.name}”删除后无法恢复，历史任务仍会保留创建时的品牌快照。</p>
              <div className="brand-delete-modal-actions">
                <button type="button" onClick={() => setBrandDeleteId(null)} disabled={deletingBrand} autoFocus>取消</button>
                <button type="button" className="is-danger" onClick={() => { if (detail) void removeBrand(detail) }} disabled={deletingBrand}>
                  {deletingBrand ? '删除中…' : '确认删除'}
                </button>
              </div>
            </div>
          </div>
        ) : null}
      </div>
    </section>
  )
}
