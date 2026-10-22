import type { Asset, BrandMode, BrandStyle } from '../types'

type Props = {
  brands: BrandStyle[]
  selectedBrandId: string
  brandsLoading: boolean
  brandsError: string | null
  mode: BrandMode
  references: Asset[]
  referencesUploading: boolean
  busy: boolean
  onModeChange: (mode: BrandMode) => void
  onBrandChange: (brandId: string) => void
  onUploadReferences: (files: File[]) => void
  onRemoveReference: (assetId: string) => void
}

const MODE_OPTIONS: Array<{ value: BrandMode; label: string; hint: string }> = [
  { value: 'preset', label: '预设风格', hint: '使用品牌规范' },
  { value: 'reference', label: '参考图', hint: '上传风格参考' },
  { value: 'none', label: '无', hint: '不限制风格' },
]

export default function BrandModeSelector({
  brands,
  selectedBrandId,
  brandsLoading,
  brandsError,
  mode,
  references,
  referencesUploading,
  busy,
  onModeChange,
  onBrandChange,
  onUploadReferences,
  onRemoveReference,
}: Props) {
  return (
    <div className="brand-field">
      <div className="brand-field-head">
        <span className="prompt-field-label">
          品牌风格 <span>可选</span>
        </span>
        <span className="brand-field-hint">选择预设、参考图或不限制风格</span>
      </div>

      <div className="brand-mode-options" role="radiogroup" aria-label="品牌风格模式">
        {MODE_OPTIONS.map((option) => (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={mode === option.value}
            className={`brand-mode-option${mode === option.value ? ' is-active' : ''}`}
            onClick={() => onModeChange(option.value)}
            disabled={busy}
          >
            <strong>{option.label}</strong>
            <span>{option.hint}</span>
          </button>
        ))}
      </div>

      {mode === 'preset' ? (
        brandsError ? (
          <div className="brand-empty brand-error" role="alert">
            <strong>品牌风格加载失败</strong>
            <span>{brandsError}</span>
          </div>
        ) : brandsLoading ? (
          <div className="brand-empty">
            <span className="brand-loading-dot" aria-hidden="true" />
            <span>正在读取品牌配置</span>
          </div>
        ) : brands.length === 0 ? (
          <div className="brand-empty">
            <strong>没有可用的预设风格</strong>
            <span>可切换到“参考图”或“无”。</span>
          </div>
        ) : (
          <div className="brand-options" role="radiogroup" aria-label="预设品牌风格">
            {brands.map((brand) => {
              const selected = brand.id === selectedBrandId
              return (
                <button
                  key={brand.id}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  className={`brand-option${selected ? ' is-active' : ''}`}
                  onClick={() => onBrandChange(brand.id)}
                  disabled={busy}
                >
                  <span className="brand-cover">
                    {brand.cover_url ? (
                      <img src={brand.cover_url} alt={`${brand.name} 风格参考`} />
                    ) : (
                      <span className="brand-cover-placeholder">{brand.short_name.slice(0, 2)}</span>
                    )}
                    {selected ? <span className="brand-selected-mark">当前</span> : null}
                  </span>
                  <span className="brand-option-copy">
                    <strong>{brand.name}</strong>
                    <span>{brand.summary || '品牌专属视觉规范'}</span>
                  </span>
                </button>
              )
            })}
          </div>
        )
      ) : null}

      {mode === 'reference' ? (
        <div className="style-reference-field">
          <div className="style-reference-head">
            <span>风格参考图 <em>可选</em></span>
            <span>{references.length} / 3</span>
          </div>
          <div className="style-reference-grid">
            {references.map((asset) => (
              <div className="style-reference-item" key={asset.id}>
                <img src={asset.url} alt={asset.original_name} />
                <button
                  type="button"
                  onClick={() => onRemoveReference(asset.id)}
                  aria-label={`移除 ${asset.original_name}`}
                  disabled={busy || referencesUploading}
                >
                  ×
                </button>
              </div>
            ))}
            {references.length < 3 ? (
              <label className="style-reference-add">
                <input
                  type="file"
                  multiple
                  accept="image/jpeg,image/png,image/apng,image/webp,image/gif,image/bmp,image/tiff,image/avif,image/heic,image/heif,image/jp2,image/x-icon,image/x-tga,.jpg,.jpeg,.jfif,.png,.apng,.webp,.gif,.bmp,.dib,.tif,.tiff,.avif,.avifs,.heic,.heif,.jp2,.j2k,.jpf,.jpx,.ico,.tga,.vda,.vst"
                  disabled={busy || referencesUploading}
                  onChange={(event) => {
                    const files = Array.from(event.target.files ?? []).slice(0, 3 - references.length)
                    if (files.length) onUploadReferences(files)
                    event.target.value = ''
                  }}
                />
                <span>{referencesUploading ? '上传中…' : '＋ 上传参考图'}</span>
              </label>
            ) : null}
          </div>
          <p>参考图只影响配色、构图、字体气质和氛围，可不上传。</p>
        </div>
      ) : null}
    </div>
  )
}
