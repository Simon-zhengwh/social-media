import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  ApiError,
  createBrand,
  getBrandRules,
  updateBrandRules,
  previewBrand,
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
  BrandStyleFields,
  BrandRules,
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
  schema_version: number
  style_fields: BrandStyleFields | null
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

const emptyStyle = (): BrandStyleFields => ({
  palette_notes: '', typography: '', subject_style: '', background: '',
  layout: '重点清晰、层级明确，正文不遮挡关键主体', decoration: '', identity: '', restrictions: '',
})

const styleFields: { key: keyof BrandStyleFields; label: string; hint: string; example: string; required?: boolean }[] = [
  { key: 'palette_notes', label: '配色说明', hint: '说明颜色的主次、用途和允许的变化；与色值至少填一项。', example: '深绿为主色，奶油白作背景，红色强调优惠；允许同色系变化。' },
  { key: 'typography', label: '字体风格', hint: '分别描述标题、正文和数字的字体气质。', example: '标题用粗笔刷字，正文用清晰无衬线字，价格数字醒目。', required: true },
  { key: 'subject_style', label: '主体表现', hint: '描述商品、人物等主体的表现方式。', example: '真实菜品摄影拼贴，保持食物细节和自然色泽。', required: true },
  { key: 'background', label: '背景与质感', hint: '描述背景颜色、材质和氛围。', example: '奶油白背景，带轻微纸张纹理。', required: true },
  { key: 'layout', label: '排版偏好', hint: '说明视觉重点、文字层级和疏密偏好，可调整默认建议。', example: '菜品为主，标题放大，优惠条件放在次级区域。' },
  { key: 'decoration', label: '装饰元素', hint: '不需要装饰时可明确填写“无”。', example: '少量手写箭头、撕纸边缘和促销色块。' },
  { key: 'identity', label: '固定识别元素', hint: '填写品牌固定角色或其他视觉标记。', example: '白布幽灵角色，佩戴深色墨镜。' },
  { key: 'restrictions', label: '专属禁忌与补充', hint: '补充该风格特有的限制。', example: '不使用霓虹效果，不添加未定稿角色。' },
]

const ruleFields: { key: Exclude<keyof BrandRules, 'version'>; label: string; hint: string }[] = [
  { key: 'copy_accuracy', label: '文案准确', hint: '保证价格、日期、条件和承诺准确。' },
  { key: 'asset_usage', label: '素材使用', hint: '区分内容素材与风格参考的用途。' },
  { key: 'logo_usage', label: 'Logo 使用', hint: '保持品牌标识的图形、字形、比例和原色。' },
  { key: 'readability', label: '可读性', hint: '避免遮挡关键主体或裁切必要信息。' },
]

function toDraft(detail: BrandDetail): BrandDraft {
  return {
    schema_version: detail.schema_version ?? 1,
    style_fields: detail.style_fields ? { ...detail.style_fields } : null,
    name: detail.name,
    short_name: detail.short_name,
    summary: detail.summary,
    platform_id: detail.platform_id,
    placement_id: detail.placement_id,
    colors: detail.colors.map((color) => ({ ...color })),
    logo_asset_ids: [...detail.logo_asset_ids],
    layout_guide: detail.layout_guide,
    background_guide: detail.background_guide,
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

function CommonRulesCard({ onOpen }: { onOpen: () => void }) {
  return (
    <button
      type="button"
      className="brand-common-rules-card"
      onClick={onOpen}
    >
      <span className="brand-common-rules-mark" aria-hidden="true">规</span>
      <span className="brand-common-rules-copy">
        <span className="brand-common-rules-title">
          <strong>通用规范</strong>
          <span className="brand-common-rules-chip">适用于全部品牌风格</span>
        </span>
        <span className="brand-common-rules-description">
          文案准确 · 素材使用 · Logo · 可读性
        </span>
      </span>
      <span className="brand-common-rules-action">查看与编辑 <span aria-hidden="true">→</span></span>
    </button>
  )
}

export default function BrandStudio({
  brands,
  platforms,
  brandsLoading,
  brandsError,
  onRefreshBrands,
}: Props) {
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [showRules, setShowRules] = useState(false)
  const [rules, setRules] = useState<BrandRules | null>(null)
  const [rulesLoading, setRulesLoading] = useState(false)
  const [rulesSaving, setRulesSaving] = useState(false)
  const [rulesError, setRulesError] = useState<string | null>(null)
  const [rulesNotice, setRulesNotice] = useState<string | null>(null)
  const [preview, setPreview] = useState<string | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const previewRevision = useRef(0)
  const [detail, setDetail] = useState<BrandDetail | null>(null)
  const [draft, setDraft] = useState<BrandDraft | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [uploadingLogo, setUploadingLogo] = useState(false)
  const [uploadingReference, setUploadingReference] = useState(false)
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<{ target: 'logo' | 'reference'; id: string } | null>(null)
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

  const invalidatePreview = () => {
    previewRevision.current += 1
    setPreview(null)
    setPreviewing(false)
  }

  const openBrand = async (brandId: string) => {
    setCreating(false)
    invalidatePreview()
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
    invalidatePreview()
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
    invalidatePreview()
    setDraft((previous) => (previous ? { ...previous, ...patch } : previous))
  }

  const payloadFromDraft = (source: BrandDraft) => ({
    ...source,
    name: source.name.trim(),
    short_name: source.name.trim().slice(0, 40),
    summary: source.summary.trim(),
    platform: '',
    note: source.note,
    layout_guide: source.style_fields ? '' : source.layout_guide.trim(),
    background_guide: source.style_fields ? '' : source.background_guide.trim(),
  })

  const saveBrand = async () => {
    if (!draft) return
    invalidatePreview()
    if (!draft.name.trim() || !draft.summary.trim() || !draft.platform_id || !draft.placement_id) {
      setError('请填写风格名称、风格定位、适用平台和发布用途')
      return
    }
    if (draft.style_fields && (!draft.style_fields.typography.trim() || !draft.style_fields.subject_style.trim() || !draft.style_fields.background.trim() || (!draft.colors.length && !draft.style_fields.palette_notes.trim()))) {
      setError('请填写字体风格、主体表现、背景与质感，并至少填写配色说明或一个色值')
      return
    }
    setSaving(true)
    setError(null)
    setNotice(null)
    try {
      const data = selectedId
        ? await updateBrand(selectedId, payloadFromDraft(draft))
        : await createBrand(payloadFromDraft(draft))
      setSelectedId(data.id)
      setCreating(false)
      applyDetail(data)
      setNotice(selectedId ? '品牌配置已保存' : '风格已创建，现在可以上传 Logo 和参考图')
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
      invalidatePreview()
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
      applyDetail(data, false)
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
      applyDetail(await deleteBrandLogo(selectedId, assetId), false)
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

  const loadRules = async () => {
    setRulesLoading(true)
    setRulesError(null)
    setRulesNotice(null)
    try {
      setRules(await getBrandRules())
    } catch (reason) {
      setRulesError(errorText(reason))
    } finally {
      setRulesLoading(false)
    }
  }

  const saveRules = async () => {
    if (!rules) return
    if (ruleFields.some(({ key }) => !rules[key].trim())) {
      setRulesError('请填写四项通用规范')
      return
    }
    setRulesSaving(true)
    setRulesError(null)
    setRulesNotice(null)
    try {
      setRules(await updateBrandRules(rules))
      setRulesNotice('通用规范已保存，将应用于之后新建的生成任务')
      invalidatePreview()
    } catch (reason) {
      setRulesError(reason instanceof ApiError && (reason.code === 'http_409' || reason.code === 'version_conflict')
        ? '通用规范已被其他页面更新，本次修改未保存。请返回后重新打开再修改。'
        : errorText(reason))
    } finally {
      setRulesSaving(false)
    }
  }

  if (showRules) {
    return (
      <section className="asset-page brand-page brand-detail-page">
        <div className="brand-detail-shell">
          <button type="button" className="brand-back" onClick={() => setShowRules(false)}>← 返回品牌列表</button>
          <div className="brand-section-head"><div><span className="eyebrow">COMMON RULES</span><h1>通用规范</h1></div><span>版本 {rules?.version ?? '—'}</span></div>
          <p className="panel-lead">所有风格、参考图风格和无品牌模式都会应用。保存后对新任务生效，已有任务保留原规范。</p>
          {rulesError ? <div className="asset-notice asset-notice-error" role="alert">{rulesError}</div> : null}
          {rulesNotice ? <div className="asset-notice" role="status">{rulesNotice}</div> : null}
          {rulesLoading ? <p>正在读取通用规范…</p> : rules ? (
            <div className="brand-form-grid">
              {ruleFields.map(({ key, label, hint }) => (
                <label className="brand-field brand-field-wide" key={key}>
                  <span>{label} <span className="asset-required" aria-hidden="true">*</span></span><small>{hint}</small>
                  <textarea required rows={4} maxLength={4000} value={rules[key]} onChange={(event) => setRules({ ...rules, [key]: event.target.value })} />
                </label>
              ))}
            </div>
          ) : null}
          <div className="brand-detail-footer-actions">
            <button type="button" className="mp-btn-primary" disabled={!rules || rulesLoading || rulesSaving} onClick={() => void saveRules()}>{rulesSaving ? '保存中…' : '保存通用规范'}</button>
          </div>
        </div>
      </section>
    )
  }

  if (!selectedId && !creating) {
    return (
      <section className="asset-page brand-page">
        <div className="asset-head asset-head-actions">
          <div className="asset-stats">
            <button type="button" className="mp-btn-primary" onClick={() => {
              setCreating(true)
              setDetail(null)
              setError(null)
              setNotice(null)
              invalidatePreview()
              setEditingColor(null)
              setDraft({ schema_version: 2, style_fields: emptyStyle(), name: '', short_name: '', summary: '', platform_id: '', placement_id: '', colors: [], logo_asset_ids: [], layout_guide: '', background_guide: '', logo_keywords: [], note: '', is_default: false, cover_asset_id: null })
            }}>＋ 新建风格</button>
          </div>
        </div>
        {brandsError ? <div className="asset-notice asset-notice-error">{brandsError}</div> : null}
        {error ? <div className="asset-notice asset-notice-error">{error}</div> : null}
        {notice ? <div className="asset-notice">{notice}</div> : null}
        <CommonRulesCard onOpen={() => { setShowRules(true); void loadRules() }} />
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
        ) : (
          <>
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
            {brands.length === 0 ? (
              <p className="brand-empty-note">还没有独立品牌风格，点击“新建风格”开始配置。</p>
            ) : null}
          </>
        )}
      </section>
    )
  }

  return (
    <section className="asset-page brand-page brand-detail-page">
      <div className="brand-detail-shell">
        <div className="brand-detail-head">
          <button type="button" className="brand-back" onClick={() => { invalidatePreview(); setSelectedId(null); setCreating(false); setDetail(null); setDraft(null); setError(null); setNotice(null) }}>
            ← 返回品牌列表
          </button>
          <div className="brand-detail-title">
            <div>
              <h1>{draft?.name || detail?.name || '新建风格'}</h1>
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
                  <span>风格名称 <span className="asset-required" aria-hidden="true">*</span></span>
                  <small>用名称区分商家及其视觉风格。</small>
                  <input placeholder="例如：喜聚 · 暖调复古" required maxLength={80} value={draft.name} onChange={(event) => updateDraft({ name: event.target.value })} />
                </label>

                <label className="brand-field">
                  <span>适用平台 <span className="asset-required" aria-hidden="true">*</span></span>
                  <select required value={draft.platform_id} onChange={(event) => {
                    const next = platforms.find((item) => item.id === event.target.value)
                    updateDraft({ platform_id: event.target.value, placement_id: next?.default_placement_id ?? '' })
                  }}>
                    <option value="" disabled>请选择平台</option>
                    {platforms.map((platform) => <option key={platform.id} value={platform.id}>{platform.name}</option>)}
                  </select>
                </label>
                <label className="brand-field">
                  <span>发布用途 <span className="asset-required" aria-hidden="true">*</span></span>
                  <select required value={draft.placement_id} disabled={!selectedPlatform} onChange={(event) => updateDraft({ placement_id: event.target.value })}>
                    {selectedPlatform ? selectedPlatform.placements.map((placement) => <option key={placement.id} value={placement.id}>{placement.name}</option>) : <option value="">请选择用途</option>}
                  </select>
                </label>
                <label className="brand-field brand-field-wide">
                  <span>风格定位 <span className="asset-required" aria-hidden="true">*</span></span>
                  <small>用几个关键词概括整体气质。</small>
                  <textarea rows={2} required maxLength={500} placeholder="例如：温暖、复古、轻手作感" value={draft.summary} onChange={(event) => updateDraft({ summary: event.target.value })} />
                </label>
              </div>

              <div className="brand-visual-assets">
                <div className="brand-asset-group">
                  <div className="brand-subhead"><strong>品牌 Logo</strong><span>{detail?.logo_images.length ?? 0}/5</span></div>
                  <div className="brand-logo-strip">
                    {detail?.logo_images.map((logo) => (
                      <div className="brand-logo-chip" key={logo.asset_id}>
                        <ZoomableImage
                          className="brand-logo-preview"
                          src={logo.url}
                          alt={logo.original_name}
                          title={logo.original_name}
                        />
                        <button type="button" className="brand-logo-remove" onClick={() => setConfirmDelete({ target: 'logo', id: logo.asset_id })} aria-label="移除 Logo" disabled={deletingId === logo.asset_id}>×</button>
                      </div>
                    ))}
                    {!logosFull ? (
                      <label className="brand-asset-add">
                        <input type="file" multiple accept="image/png,image/jpeg,image/webp" disabled={uploadingLogo || !selectedId} onChange={(event) => { const files = Array.from(event.target.files ?? []); if (files.length) void uploadLogos(files); event.target.value = '' }} />
                        <span>＋</span><small>{!selectedId ? '保存后上传' : uploadingLogo ? '上传中' : 'Logo'}</small>
                      </label>
                    ) : null}
                  </div>
                </div>

                <div className="brand-asset-group">
                  <div className="brand-subhead"><strong>主色调</strong><span>与配色说明至少填一项 · {draft.colors.length}/6</span></div>
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
                <div><span className="eyebrow">STYLE TEMPLATE</span><h2>风格填写模板</h2></div>
              </div>
              <p className="brand-template-hint">只需填写这套风格独有的要求。文案准确、素材使用、Logo 和可读性由通用规范统一提供。示例仅供参考，不会自动保存。</p>
              {draft.style_fields ? (
                <div className="brand-form-grid">
                  {styleFields.map(({ key, label, hint, example, required }) => (
                    <label className="brand-field" key={key}>
                      <span>{label}{required ? <> <span className="asset-required" aria-hidden="true">*</span></> : key === 'palette_notes' ? ' · 配色二选一' : null}</span>
                      <small>{hint}</small>
                      <textarea rows={3} required={required} maxLength={key === 'restrictions' ? 6000 : 4000} placeholder={`例如：${example}`} value={draft.style_fields![key]} onChange={(event) => updateDraft({ style_fields: { ...draft.style_fields!, [key]: event.target.value } })} />
                    </label>
                  ))}
                </div>
              ) : (
                <>
                  <p className="brand-template-hint">这套风格尚未转换为填写模板，原规范仍有效。转换后请整理并填齐必填项；原文将保留供查阅。</p>
                  <button type="button" className="mp-btn-ghost" onClick={() => updateDraft({ schema_version: 2, style_fields: emptyStyle() })}>转换为填写模板</button>
                  <label className="brand-field"><span>原布局规范</span><textarea rows={7} value={draft.layout_guide} onChange={(event) => updateDraft({ layout_guide: event.target.value })} /></label>
                  <label className="brand-field"><span>原背景规范</span><textarea rows={5} value={draft.background_guide} onChange={(event) => updateDraft({ background_guide: event.target.value })} /></label>
                </>
              )}
              {detail && (detail.legacy_guides || (!detail.style_fields && draft.style_fields)) ? (
                <details className="brand-guides-fold">
                  <summary>原始规范 · 只读留存</summary>
                  <pre>{(detail.legacy_guides ?? detail).layout_guide}</pre>
                  <pre>{(detail.legacy_guides ?? detail).background_guide}</pre>
                </details>
              ) : null}
              <details className="brand-guides-fold">
                <summary>规范预览 · 通用规范、当前风格与平台规则</summary>
                <p className="brand-template-hint">按当前未保存的表单生成预览。修改表单后请重新预览。</p>
                <button type="button" className="mp-btn-ghost" disabled={previewing} onClick={async () => {
                  const revision = ++previewRevision.current
                  setPreviewing(true)
                  setError(null)
                  try {
                    const result = await previewBrand(payloadFromDraft(draft))
                    if (revision === previewRevision.current) setPreview(result.prompt)
                  } catch (reason) {
                    if (revision === previewRevision.current) setError(errorText(reason))
                  } finally {
                    if (revision === previewRevision.current) setPreviewing(false)
                  }
                }}>{previewing ? '读取中…' : '生成规范预览'}</button>
                {preview ? <pre>{preview}</pre> : null}
              </details>
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
                      <button type="button" className="brand-reference-delete" onClick={() => setConfirmDelete({ target: 'reference', id: reference.asset_id })} disabled={deletingId === reference.asset_id}>删除</button>
                    </div>
                  </article>
                ))}
                {!referencesFull ? (
                  <label className={`brand-reference-upload${uploadingReference ? ' is-disabled' : ''}`}>
                    <input type="file" accept="image/png,image/jpeg,image/webp" disabled={uploadingReference || !selectedId} onChange={(event) => { const file = event.target.files?.[0]; if (file) void uploadReference(file); event.target.value = '' }} />
                    <span className="upload-plus" aria-hidden="true" /><strong>{!selectedId ? '保存风格后上传' : uploadingReference ? '上传中…' : '上传参考图'}</strong><span>PNG / JPG / WebP</span>
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
                      移除{confirmDelete.target === 'logo' ? '品牌 Logo' : '参考图'}？
                    </strong>
                    <p>确认从当前风格中移除？素材文件和历史任务会保留。</p>
                    <div className="brand-delete-modal-actions">
                      <button type="button" onClick={() => setConfirmDelete(null)} autoFocus>取消</button>
                      <button type="button" className="is-danger" onClick={() => {
                        if (confirmDelete.target === 'logo') void removeLogo(confirmDelete.id)
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
                <button type="button" className="mp-btn-primary" onClick={() => void saveBrand()} disabled={saving}>{saving ? '保存中…' : creating ? '创建风格' : '保存品牌配置'}</button>
              </div>
            </div>
          </div>
        )}

        {brandDeleteId && brandDeleteId === selectedId ? (
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
