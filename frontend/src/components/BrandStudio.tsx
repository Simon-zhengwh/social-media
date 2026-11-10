import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  ApiError,
  createBrand,
  getBrandRules,
  updateBrandRules,
  deleteBrand,
  duplicateBrand,
  deleteBrandLogo,
  deleteBrandReference,
  getBrand,
  initializeBrandFromProfile,
  updateBrand,
  uploadBrandLogos,
  uploadBrandReference,
} from '../api'
import type {
  BrandColor,
  BrandDetail,
  BrandLogoImage,
  BrandStyle,
  BrandStyleFields,
  BrandRules,
  PlatformPreset,
} from '../types'
import { ZoomableImage } from './ImageLightbox'
import MerchantProfilePanel, { MerchantBrandInfoCard } from './MerchantProfilePanel'

type Props = {
  brands: BrandStyle[]
  // Merchant-level logos: shared by every style, edited here once.
  merchantLogos: BrandLogoImage[]
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
  layout_guide: string
  background_guide: string
  logo_keywords: string[]
  note: string
  is_default: boolean
  cover_asset_id: string | null
}

const ruleFields: { key: Exclude<keyof BrandRules, 'version'>; label: string; hint: string }[] = [
  { key: 'copy_accuracy', label: '文案准确', hint: '保证价格、日期、条件和承诺准确。' },
  { key: 'asset_usage', label: '素材使用', hint: '限定裁切、等比缩放、移动、旋转及小于 30° 的轻微透视；禁止重绘或编造素材。' },
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
  merchantLogos,
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
  const [detail, setDetail] = useState<BrandDetail | null>(null)
  const [draft, setDraft] = useState<BrandDraft | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [uploadingLogo, setUploadingLogo] = useState(false)
  const [uploadingReference, setUploadingReference] = useState(false)
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<{ target: 'logo' | 'reference'; id: string } | null>(null)
  const [seeding, setSeeding] = useState(false)
  const assetDeleteDialogRef = useRef<HTMLDialogElement>(null)
  const [coverBusyId, setCoverBusyId] = useState<string | null>(null)
  const [editingColor, setEditingColor] = useState<number | null>(null)
  const [colorInput, setColorInput] = useState('')
  const [brandDeleteId, setBrandDeleteId] = useState<string | null>(null)
  const [deletingBrand, setDeletingBrand] = useState(false)
  const [duplicateOpen, setDuplicateOpen] = useState(false)
  const [duplicateName, setDuplicateName] = useState('')
  const [duplicatePlatformId, setDuplicatePlatformId] = useState('')
  const [duplicatePlacementId, setDuplicatePlacementId] = useState('')
  const [duplicateError, setDuplicateError] = useState<string | null>(null)
  const [duplicating, setDuplicating] = useState(false)
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
  const duplicatePlatform = platforms.find((item) => item.id === duplicatePlatformId)
  const logosFull = merchantLogos.length >= 5
  const referencesFull = (detail?.reference_images.length ?? 0) >= 5

  const openBrand = async (brandId: string) => {
    setCreating(false)
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
    short_name: source.name.trim().slice(0, 40),
    summary: source.summary.trim(),
    platform: '',
    note: source.note,
    layout_guide: source.style_fields ? '' : source.layout_guide.trim(),
    background_guide: source.style_fields ? '' : source.background_guide.trim(),
  })

  /*
    Build the merchant's first style from its AM brand profile. The profile is what the merchant
    panel already shows, so this only saves the operator a blank form — the visual fields still
    need their own review against the reference images they upload.
  */
  // Either card is enough to need the two-column workspace; the condition has to match
  // what is rendered below, or the cards land in a single-column body.
  const hasMerchantSidebar = Boolean(
    detail?.merchant_profile?.brand_info ||
    detail?.merchant_profile?.prompt_sections?.length ||
    merchantLogos.length,
  )
  const seedFromProfile = async () => {
    setSeeding(true)
    setError(null)
    setNotice(null)
    try {
      const data = await initializeBrandFromProfile()
      setSelectedId(data.id)
      setCreating(false)
      applyDetail(data)
      setNotice(
        data.created
          ? '已从商户资料生成风格，请补充主色调与参考图后保存'
          : '该商户已有风格，已为你打开',
      )
    } catch (reason: unknown) {
      setError(errorText(reason))
    } finally {
      setSeeding(false)
    }
  }

  const saveBrand = async () => {
    if (!draft) return
    if (!draft.name.trim() || !draft.platform_id || !draft.placement_id) {
      setError('请填写风格名称、适用平台和发布用途')
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
      setNotice(selectedId ? '品牌配置已保存' : '风格已创建，现在可以上传参考图和主色调')
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
  const openDuplicate = () => {
    if (!draft) return
    setDuplicateName(`${draft.name} 副本`)
    setDuplicatePlatformId(draft.platform_id)
    setDuplicatePlacementId(draft.placement_id)
    setDuplicateError(null)
    setDuplicateOpen(true)
  }

  const submitDuplicate = async () => {
    if (!selectedId) return
    if (!duplicateName.trim() || !duplicatePlatformId || !duplicatePlacementId) {
      setDuplicateError('请填写副本名称、适用平台和发布用途')
      return
    }
    setDuplicating(true)
    setDuplicateError(null)
    try {
      const data = await duplicateBrand(selectedId, {
        name: duplicateName.trim(),
        platform_id: duplicatePlatformId,
        placement_id: duplicatePlacementId,
      })
      await onRefreshBrands()
      setSelectedId(data.id)
      setCreating(false)
      applyDetail(data)
      setDuplicateOpen(false)
      setNotice(`已复制为“${data.name}”，可继续独立编辑`)
    } catch (reason: unknown) {
      setDuplicateError(errorText(reason))
    } finally {
      setDuplicating(false)
    }
  }
  const uploadLogos = async (files: File[]) => {
    const accepted = files.slice(0, Math.max(0, 5 - merchantLogos.length))
    if (!accepted.length) return
    setUploadingLogo(true)
    setError(null)
    setNotice(null)
    try {
      // Uploads land in the asset library and are then attached to the merchant-level logo set.
      const before = merchantLogos.length
      const data = await uploadBrandLogos(accepted)
      await onRefreshBrands()
      setNotice(`已上传 ${data.logo_images.length - before} 张 Logo`)
    } catch (reason: unknown) {
      setError(errorText(reason))
    } finally {
      setUploadingLogo(false)
    }
  }

  const removeLogo = async (assetId: string) => {
    setDeletingId(assetId)
    setConfirmDelete(null)
    setError(null)
    try {
      await deleteBrandLogo(assetId)
      await onRefreshBrands()
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
              setEditingColor(null)
              setDraft({ schema_version: 2, style_fields: null, name: '', short_name: '', summary: '', platform_id: '', placement_id: '', colors: [], layout_guide: '', background_guide: '', logo_keywords: [], note: '', is_default: false, cover_asset_id: null })
            }}>＋ 新建风格</button>
            {!brandsLoading && brands.length === 0 ? (
              <button
                type="button"
                className="mp-btn-ghost"
                onClick={() => void seedFromProfile()}
                disabled={seeding}
              >
                {seeding ? '生成中…' : '从商户资料生成风格'}
              </button>
            ) : null}
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
                    {brand.note ? <span className="brand-card-summary">{brand.note}</span> : null}
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
              <p className="brand-empty-note">还没有品牌风格。可以“从商户资料生成风格”快速起步，或点击“新建风格”手工配置。</p>
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
          <button type="button" className="brand-back" onClick={() => { setSelectedId(null); setCreating(false); setDetail(null); setDraft(null); setError(null); setNotice(null) }}>
            ← 返回品牌列表
          </button>
          <div className="brand-detail-title">
            <div>
              <h1>{detail?.merchant_profile?.name || draft?.name || detail?.name || '新建风格'}</h1>
            </div>

          </div>
        </div>

        {error ? <div className="asset-notice asset-notice-error">{error}</div> : null}
        {notice ? <div className="asset-notice">{notice}</div> : null}


        {loading || !draft ? (
          <div className="asset-empty"><strong>正在读取品牌配置</strong><span>请稍候…</span></div>
        ) : (
          <div className={`brand-detail-body${hasMerchantSidebar ? ' brand-merchant-workspace' : ''}`}>
            <div className="brand-left-column">
              {detail?.merchant_profile?.brand_info ? (
                <MerchantBrandInfoCard info={detail.merchant_profile.brand_info} />
              ) : null}
              {creating ? null : (
                <section className="brand-form-section brand-logo-card">
                  <div className="brand-section-head">
                    <div>
                      <span className="eyebrow">MERCHANT LOGO</span>
                      <h2>品牌 Logo</h2>
                    </div>
                    <span className="brand-logo-count">{merchantLogos.length}/5</span>
                  </div>
                  <p className="brand-logo-hint">
                    商户通用字段：全部平台风格与海报共用这套 Logo，生成页默认引用第一张。
                  </p>
                  <div className="brand-logo-strip">
                    {merchantLogos.map((logo) => (
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
                        <input type="file" multiple accept="image/png,image/jpeg,image/webp" disabled={uploadingLogo} onChange={(event) => { const files = Array.from(event.target.files ?? []); if (files.length) void uploadLogos(files); event.target.value = '' }} />
                        <span>＋</span><small>{uploadingLogo ? '上传中' : 'Logo'}</small>
                      </label>
                    ) : null}
                  </div>
                  {merchantLogos.length === 0 ? (
                    <p className="brand-logo-empty">还没有商户 Logo，上传后会直接用于全部平台风格的生成。</p>
                  ) : null}
                </section>
              )}
              <MerchantProfilePanel profile={detail?.merchant_profile ?? null} />
            </div>
            <div className="brand-platform-column">
            <section className="brand-form-section brand-basics-section">
              <div className="brand-section-head">
                <div><span className="eyebrow">PLATFORM STYLE</span><h2>平台视觉规范</h2></div>
              </div>

              {creating ? (
                <label className="brand-field brand-new-style-name">
                  <span>风格名称 <span className="asset-required" aria-hidden="true">*</span></span>
                  <small>用于在风格列表中识别这套配置。</small>
                  <input placeholder="例如：喜聚 · 暖调复古" required maxLength={80} value={draft.name} onChange={(event) => updateDraft({ name: event.target.value })} />
                </label>
              ) : null}

              <div className="brand-form-grid brand-basics-grid">
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
              </div>

              <div className="brand-visual-assets">
                <div className="brand-asset-group">
                  <div className="brand-subhead"><strong>主色调</strong><span>{draft.colors.length}/6</span></div>
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
              <label className="brand-field brand-field-wide">
                <span>备注</span>
                <small>选填，简单写下这套风格的要求即可。</small>
                <textarea rows={4} maxLength={4000} placeholder="例如：整体简洁一些，标题醒目，多留白。" value={draft.note} onChange={(event) => updateDraft({ note: event.target.value })} />
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
                    <p>
                      {confirmDelete.target === 'logo'
                        ? '确认从商户 Logo 集合移除？全部平台风格将不再默认使用它，素材文件和历史任务会保留。'
                        : '确认从当前风格中移除？素材文件和历史任务会保留。'}
                    </p>
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
                {!creating ? (
                  <button type="button" className="brand-duplicate-button" onClick={openDuplicate} disabled={!detail || saving || duplicating}>
                    复制为独立风格
                  </button>
                ) : null}
                <button type="button" className="mp-btn-primary" onClick={() => void saveBrand()} disabled={saving}>{saving ? '保存中…' : creating ? '创建风格' : '保存品牌配置'}</button>
              </div>
            </div>
            </div>
          </div>
        )}

        {duplicateOpen ? (
          <div
            className="brand-delete-modal-backdrop"
            onMouseDown={(event) => {
              if (event.target === event.currentTarget && !duplicating) setDuplicateOpen(false)
            }}
          >
            <div
              className="brand-delete-modal brand-duplicate-modal"
              role="dialog"
              aria-modal="true"
              aria-labelledby="brand-duplicate-title"
              onMouseDown={(event) => event.stopPropagation()}
              onKeyDown={(event) => {
                if (event.key === 'Escape' && !duplicating) setDuplicateOpen(false)
              }}
            >
              <strong id="brand-duplicate-title">复制为独立风格</strong>
              <p>复制视觉规范和素材引用，再为副本选择独立平台与发布用途。商户资料仍仅从当前 AM 商家实时读取。</p>
              <div className="brand-form-grid brand-duplicate-fields">
                <label className="brand-field brand-field-wide">
                  <span>副本名称 <span className="asset-required" aria-hidden="true">*</span></span>
                  <input autoFocus required maxLength={80} value={duplicateName} onChange={(event) => setDuplicateName(event.target.value)} />
                </label>
                <label className="brand-field">
                  <span>适用平台 <span className="asset-required" aria-hidden="true">*</span></span>
                  <select required value={duplicatePlatformId} onChange={(event) => {
                    const next = platforms.find((item) => item.id === event.target.value)
                    setDuplicatePlatformId(event.target.value)
                    setDuplicatePlacementId(next?.default_placement_id ?? '')
                  }}>
                    <option value="" disabled>请选择平台</option>
                    {platforms.map((platform) => <option key={platform.id} value={platform.id}>{platform.name}</option>)}
                  </select>
                </label>
                <label className="brand-field">
                  <span>发布用途 <span className="asset-required" aria-hidden="true">*</span></span>
                  <select required value={duplicatePlacementId} disabled={!duplicatePlatform} onChange={(event) => setDuplicatePlacementId(event.target.value)}>
                    {duplicatePlatform ? duplicatePlatform.placements.map((placement) => <option key={placement.id} value={placement.id}>{placement.name}</option>) : <option value="">请选择用途</option>}
                  </select>
                </label>
              </div>
              {duplicateError ? <div className="brand-duplicate-error">{duplicateError}</div> : null}
              <div className="brand-delete-modal-actions">
                <button type="button" onClick={() => setDuplicateOpen(false)} disabled={duplicating}>取消</button>
                <button type="button" className="is-primary" onClick={() => void submitDuplicate()} disabled={duplicating}>
                  {duplicating ? '复制中…' : '创建副本'}
                </button>
              </div>
            </div>
          </div>
        ) : null}
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
