import type { BrandLogoImage } from '../types'
import { ZoomableImage } from './ImageLightbox'

type Props = {
  logos: BrandLogoImage[]
  selectedLogoId: string
  busy: boolean
  logoUploading: boolean
  logoDeleting: boolean
  canAddLogo: boolean
  onUploadLogos: (files: File[]) => void
  onRemoveLogo: (assetId: string) => void
}

export default function LogoSelector({
  logos,
  selectedLogoId,
  busy,
  logoUploading,
  logoDeleting,
  canAddLogo,
  onUploadLogos,
  onRemoveLogo,
}: Props) {
  const activeLogo = logos.find((logo) => logo.asset_id === selectedLogoId) ?? null
  const activeLogoName = activeLogo?.original_name || '品牌 Logo'

  return (
    <div className="brand-field logo-selector-field">
      <div className="brand-field-head">
        <span className="prompt-field-label">
          品牌 Logo <span>可选</span>
        </span>
      </div>
      <div className="logo-selector-options" aria-label="品牌 Logo">
        {activeLogo ? (
          <div className="logo-selector-thumb">
            <ZoomableImage
              className="logo-selector-preview"
              src={activeLogo.url}
              alt={activeLogoName}
              title={activeLogoName}
            />
            <button
                type="button"
                className="logo-selector-remove"
                onClick={() => onRemoveLogo(activeLogo.asset_id)}
                disabled={busy || logoUploading || logoDeleting}
                aria-label={`删除 ${activeLogoName}`}
              >
                {logoDeleting ? '…' : '×'}
            </button>
          </div>
        ) : null}
        {canAddLogo && !activeLogo ? (
          <label
            className={`logo-selector-add${busy || logoUploading ? ' is-disabled' : ''}`}
            title="添加品牌 Logo"
          >
            <input
              type="file"
              accept="image/png,image/jpeg,image/webp"
              disabled={busy || logoUploading}
              onChange={(event) => {
                const files = Array.from(event.target.files ?? [])
                if (files.length) onUploadLogos(files)
                event.target.value = ''
              }}
            />
            <span className="logo-selector-add-plus" aria-hidden="true">＋</span>
            <small>{logoUploading ? '上传中…' : '添加'}</small>
          </label>
        ) : null}
      </div>
    </div>
  )
}