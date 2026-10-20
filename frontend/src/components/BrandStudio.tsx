import { useState } from 'react'
import {
  ApiError,
  deleteBrandReference,
  getBrand,
  updateBrand,
  uploadBrandReference,
} from '../api'
import type { BrandDetail, BrandStyle } from '../types'

type Props = {
  brands: BrandStyle[]
  brandsLoading: boolean
  brandsError: string | null
  onRefreshBrands: () => Promise<void>
}

type BrandDraft = {
  name: string
  short_name: string
  summary: string
  layout_guide: string
  background_guide: string
  logo_keywords: string[]
  is_default: boolean
  cover_asset_id: string | null
}

function toDraft(detail: BrandDetail): BrandDraft {
  return {
    name: detail.name,
    short_name: detail.short_name,
    summary: detail.summary,
    layout_guide: detail.layout_guide,
    background_guide: detail.background_guide,
    logo_keywords: [...detail.logo_keywords],
    is_default: detail.is_default,
    cover_asset_id: detail.cover_asset_id,
  }
}

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return '请求失败，请稍后重试'
}

export default function BrandStudio({
  brands,
  brandsLoading,
  brandsError,
  onRefreshBrands,
}: Props) {
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<BrandDetail | null>(null)
  const [draft, setDraft] = useState<BrandDraft | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null)
  const [coverBusyId, setCoverBusyId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const openBrand = async (brandId: string) => {
    setSelectedId(brandId)
    setLoading(true)
    setError(null)
    setNotice(null)
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

  const backToList = () => {
    setSelectedId(null)
    setDetail(null)
    setDraft(null)
    setError(null)
    setNotice(null)
    setConfirmDeleteId(null)
  }

  const applyDetail = (data: BrandDetail, resetDraft = true) => {
    setDetail(data)
    setDraft((previous) =>
      resetDraft || !previous
        ? toDraft(data)
        : { ...previous, cover_asset_id: data.cover_asset_id },
    )
    void onRefreshBrands()
  }

  const saveBrand = async () => {
    if (!selectedId || !draft) return
    setSaving(true)
    setError(null)
    setNotice(null)
    try {
      const data = await updateBrand(selectedId, {
        ...draft,
        name: draft.name.trim(),
        short_name: draft.short_name.trim(),
        summary: draft.summary.trim(),
        layout_guide: draft.layout_guide.trim(),
        background_guide: draft.background_guide.trim(),
      })
      applyDetail(data)
      setNotice('品牌配置已保存')
    } catch (reason: unknown) {
      setError(errorText(reason))
    } finally {
      setSaving(false)
    }
  }

  const updateDraft = (patch: Partial<BrandDraft>) => {
    setDraft((previous) => (previous ? { ...previous, ...patch } : previous))
  }

  const uploadReference = async (file: File) => {
    if (!selectedId) return
    setUploading(true)
    setError(null)
    setNotice(null)
    try {
      applyDetail(await uploadBrandReference(selectedId, file), false)
      setNotice('参考图已上传')
    } catch (reason: unknown) {
      setError(errorText(reason))
    } finally {
      setUploading(false)
    }
  }

  const removeReference = async (assetId: string) => {
    if (!selectedId) return
    setDeletingId(assetId)
    setConfirmDeleteId(null)
    setError(null)
    setNotice(null)
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
    setNotice(null)
    try {
      applyDetail(
        await updateBrand(selectedId, {
          ...draft,
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

  if (!selectedId) {
    return (
      <section className="asset-page brand-page">
        <div className="asset-head">
          <div>
            <span className="eyebrow">03 · 品牌库</span>
            <h1>品牌风格</h1>
            <p className="panel-lead">
              管理品牌视觉规范、生成指南和风格参考图。生成海报时会读取这里的配置。
            </p>
          </div>
          <div className="asset-stats">
            <span>
              <strong className="tnum">{brands.length}</strong> 个品牌
            </span>
          </div>
        </div>

        {brandsError ? <div className="asset-notice asset-notice-error">{brandsError}</div> : null}

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
          <div className="asset-empty">
            <strong>还没有品牌配置</strong>
            <span>请先通过导入脚本把品牌 skill 写入 MongoDB。</span>
          </div>
        ) : (
          <div className="brand-card-grid">
            {brands.map((brand) => (
                <button
                  type="button"
                  className="brand-card"
                  key={brand.id}
                  onClick={() => void openBrand(brand.id)}
                >
                  <span className="brand-card-cover">
                    {brand.cover_url ? (
                      <img src={brand.cover_url} alt={`${brand.name} 风格封面`} />
                    ) : (
                      <span className="brand-cover-placeholder">{brand.short_name.slice(0, 2)}</span>
                    )}
                    {brand.is_default ? <span className="brand-card-default">默认</span> : null}
                  </span>
                  <span className="brand-card-title">
                    <strong>{brand.name}</strong>
                  </span>
                  <span className="brand-card-summary">{brand.summary || '品牌专属视觉规范'}</span>
                  <span className="brand-card-action">查看与编辑 →</span>
                </button>
            ))}
          </div>
        )}
      </section>
    )
  }

  return (
    <section className="asset-page brand-page brand-detail-page">
      <div className="brand-detail-head">
        <button type="button" className="brand-back" onClick={backToList}>
          ← 返回品牌列表
        </button>
        <div className="brand-detail-title">
          <div>
            <span className="eyebrow">品牌配置 · {detail?.id ?? selectedId}</span>
            <h1>{draft?.name || detail?.name || '品牌详情'}</h1>
          </div>
          <div className="brand-detail-actions">
            {detail?.is_default ? <span className="brand-default-badge">默认品牌</span> : null}
            <button
              type="button"
              className="mp-btn-primary mp-btn-small"
              onClick={() => void saveBrand()}
              disabled={saving || loading || !draft}
            >
              {saving ? '保存中…' : '保存修改'}
            </button>
          </div>
        </div>
      </div>

      {error ? <div className="asset-notice asset-notice-error">{error}</div> : null}
      {notice ? <div className="asset-notice">{notice}</div> : null}

      {loading || !draft ? (
        <div className="asset-empty">
          <strong>正在读取品牌配置</strong>
          <span>请稍候…</span>
        </div>
      ) : (
        <div className="brand-detail-body">
          <section className="brand-form-section">
            <div className="brand-section-head">
              <div>
                <span className="eyebrow">BASICS</span>
                <h2>基本信息</h2>
              </div>
              <label className="brand-default-toggle">
                <input
                  type="checkbox"
                  checked={draft.is_default}
                  onChange={(event) => updateDraft({ is_default: event.target.checked })}
                />
                设为默认品牌
              </label>
            </div>
            <div className="brand-form-grid">
              <label className="brand-field">
                <span>品牌名称</span>
                <input
                  value={draft.name}
                  onChange={(event) => updateDraft({ name: event.target.value })}
                />
              </label>
              <label className="brand-field">
                <span>短名称</span>
                <input
                  value={draft.short_name}
                  onChange={(event) => updateDraft({ short_name: event.target.value })}
                />
              </label>
              <label className="brand-field brand-field-wide">
                <span>品牌简介</span>
                <textarea
                  rows={2}
                  value={draft.summary}
                  onChange={(event) => updateDraft({ summary: event.target.value })}
                />
              </label>
            </div>
          </section>

          <section className="brand-form-section">
            <div className="brand-section-head">
              <div>
                <span className="eyebrow">GUIDES</span>
                <h2>生成规范</h2>
              </div>
              <span className="brand-section-hint">会直接注入模型 prompt</span>
            </div>
            <div className="brand-guide-grid">
              <label className="brand-field">
                <span>排版 / 设计指南</span>
                <textarea
                  rows={12}
                  value={draft.layout_guide}
                  onChange={(event) => updateDraft({ layout_guide: event.target.value })}
                />
              </label>
              <label className="brand-field">
                <span>背景 / 摄影指南</span>
                <textarea
                  rows={12}
                  value={draft.background_guide}
                  onChange={(event) => updateDraft({ background_guide: event.target.value })}
                />
              </label>
            </div>
          </section>

          <section className="brand-form-section">
            <div className="brand-section-head">
              <div>
                <span className="eyebrow">REFERENCES</span>
                <h2>风格参考图</h2>
              </div>
              <span className="brand-section-hint">
                模型最多使用前 3 张 · {detail?.reference_images.length ?? 0}/3
              </span>
            </div>
            <div className="brand-reference-grid">
              {detail?.reference_images.map((reference) => (
                <article className="brand-reference-card" key={reference.asset_id}>
                  <img src={reference.url} alt={reference.original_name} />
                  <div className="brand-reference-meta">
                    <strong>{reference.original_name}</strong>
                    <span>
                      {reference.width} × {reference.height}
                    </span>
                  </div>
                  <div className="brand-reference-actions">
                    {reference.is_cover ? (
                      <span className="brand-cover-badge">当前封面</span>
                    ) : (
                      <button
                        type="button"
                        className="mp-btn-ghost mp-btn-small"
                        onClick={() => void setCover(reference.asset_id)}
                        disabled={coverBusyId !== null}
                      >
                        {coverBusyId === reference.asset_id ? '设置中…' : '设为封面'}
                      </button>
                    )}
                    {confirmDeleteId === reference.asset_id ? (
                      <span className="brand-delete-confirm">
                        <button type="button" onClick={() => setConfirmDeleteId(null)}>
                          取消
                        </button>
                        <button
                          type="button"
                          onClick={() => void removeReference(reference.asset_id)}
                          disabled={deletingId === reference.asset_id}
                        >
                          {deletingId === reference.asset_id ? '删除中…' : '确认删除'}
                        </button>
                      </span>
                    ) : (
                      <button
                        type="button"
                        className="brand-reference-delete"
                        onClick={() => setConfirmDeleteId(reference.asset_id)}
                        disabled={deletingId === reference.asset_id}
                      >
                        删除
                      </button>
                    )}
                  </div>
                </article>
              ))}
              {(detail?.reference_images.length ?? 0) < 3 ? (
                <label className={`brand-reference-upload${uploading ? ' is-disabled' : ''}`}>
                  <input
                    type="file"
                    accept="image/jpeg,image/png,image/webp"
                    disabled={uploading}
                    onChange={(event) => {
                      const file = event.target.files?.[0]
                      if (file) void uploadReference(file)
                      event.target.value = ''
                    }}
                  />
                  <span className="upload-plus" aria-hidden="true" />
                  <strong>{uploading ? '上传中…' : '上传参考图'}</strong>
                  <span>PNG / JPG / WebP</span>
                </label>
              ) : null}
            </div>
          </section>

          <div className="brand-detail-footer">
            <span>修改保存后会立即用于下一个新任务，历史任务继续使用原快照。</span>
            <button
              type="button"
              className="mp-btn-primary"
              onClick={() => void saveBrand()}
              disabled={saving}
            >
              {saving ? '保存中…' : '保存品牌配置'}
            </button>
          </div>
        </div>
      )}
    </section>
  )
}
