import type { Asset, BrandMode, BrandStyle } from '../types'
import { ZoomableImage } from './ImageLightbox'

type Props = {
  brands: BrandStyle[]
  selectedBrandId: string
  brandsLoading: boolean
  brandsError: string | null
  platformRequired: boolean
  mode: BrandMode
  references: Asset[]
  referencesUploading: boolean
  busy: boolean
  onModeChange: (mode: BrandMode) => void
  onBrandChange: (brandId: string) => void
  onUploadReferences: (files: File[]) => void
  onRemoveReference: (assetId: string) => void
}

const MODE_OPTIONS: Array<{ value: BrandMode; label: string }> = [
  { value: 'preset', label: '预设风格' },
  { value: 'reference', label: '参考图' },
  { value: 'none', label: '无' },
]

export default function BrandModeSelector({
  brands,
  selectedBrandId,
  brandsLoading,
  brandsError,
  platformRequired,
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
            <strong>{platformRequired ? '先选择发布平台和用途' : '当前平台和用途暂无匹配风格'}</strong>
            <span>选择发布条件后会自动带出对应品牌风格。</span>
          </div>
        ) : (
          <div className="brand-options" role="radiogroup" aria-label="预设品牌风格">
            {brands.map((brand) => {
              const selected = brand.id === selectedBrandId
              const displayName = brand.name.split(' · ')[0].trim() || brand.name
              return (
                <div
                  key={brand.id}
                  className={`brand-option${selected ? ' is-active' : ''}${busy ? ' is-disabled' : ''}`}
                >
                  <span className="brand-cover">
                    {brand.cover_url ? (
                      <ZoomableImage
                        className="brand-cover-image"
                        src={brand.cover_url}
                        alt={`${brand.name} 风格参考`}
                        title={brand.name}
                      />
                    ) : (
                      <span className="brand-cover-placeholder">{brand.short_name.slice(0, 2)}</span>
                    )}
                    {selected ? <span className="brand-selected-mark">当前</span> : null}
                  </span>
                  <button
                    type="button"
                    role="radio"
                    aria-checked={selected}
                    className="brand-option-select"
                    onClick={() => onBrandChange(brand.id)}
                    disabled={busy}
                  >
                    <span className="brand-option-copy">
                      <span className="brand-option-title">
                        <strong>{displayName}</strong>
                      </span>
                      <span>{brand.summary || '品牌专属视觉规范'}</span>
                      {brand.colors.length ? (
                        <span className="brand-option-palette" aria-label="品牌主色调">
                          {brand.colors.slice(0, 6).map((color) => (
                            <i
                              key={color.hex}
                              style={{ backgroundColor: color.hex }}
                              title={color.hex}
                            />
                          ))}
                        </span>
                      ) : null}
                    </span>

                  </button>
                </div>
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
                <ZoomableImage
                  className="style-reference-preview"
                  src={asset.url}
                  alt={asset.original_name}
                  title={asset.original_name}
                />
                <button
                  type="button"
                  className="style-reference-remove"
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
