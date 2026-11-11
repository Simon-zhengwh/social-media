import type { BrandLogoImage } from '../types'
import { ZoomableImage } from './ImageLightbox'

type Props = {
  /** The merchant-level logos configured on the brand page; never ordinary content assets. */
  logos: BrandLogoImage[]
  selectedLogoId: string
  busy: boolean
  /**
   * Picks one logo, or clears it when the active tile is clicked again.
   * The merchant-level set is untouched: only this poster stops using the logo.
   */
  onSelectLogo: (assetId: string) => void
}

export default function LogoSelector({ logos, selectedLogoId, busy, onSelectLogo }: Props) {
  return (
    <div className="brand-field logo-selector-field">
      <div className="brand-field-head">
        <span className="prompt-field-label">
          品牌 Logo <span>可选 · 至多一张</span>
        </span>
        {logos.length > 0 ? (
          <small className="logo-selector-hint">在「品牌风格」页维护</small>
        ) : null}
      </div>

      {logos.length === 0 ? (
        <p className="logo-selector-empty">
          还没有配置品牌 Logo。可到「品牌风格」页的商户公共区上传，上传后这里即可勾选。
        </p>
      ) : (
        <div className="logo-selector-options" aria-label="品牌 Logo">
          {logos.map((logo) => {
            const active = logo.asset_id === selectedLogoId
            const name = logo.original_name || '品牌 Logo'
            return (
              <div
                key={logo.asset_id}
                className={`logo-selector-option${active ? ' is-active' : ''}`}
              >
                <ZoomableImage
                  className="logo-selector-preview"
                  src={logo.url}
                  alt={name}
                  title={name}
                />
                <button
                  type="button"
                  className="logo-selector-choice"
                  aria-pressed={active}
                  disabled={busy}
                  onClick={() => onSelectLogo(active ? '' : logo.asset_id)}
                  title={active ? '本张海报不使用这张 Logo' : `本张海报使用 ${name}`}
                >
                  <span className="logo-selector-radio" aria-hidden="true" />
                  <span className="logo-selector-name">{name}</span>
                </button>
              </div>
            )
          })}
          <button
            type="button"
            className={`logo-selector-option logo-selector-none${selectedLogoId ? '' : ' is-active'}`}
            disabled={busy}
            onClick={() => onSelectLogo('')}
            title="本张海报不使用 Logo"
            aria-label="本张海报不使用 Logo"
          >
            <span aria-hidden="true">✕</span>
            <small>不用 Logo</small>
          </button>
        </div>
      )}
    </div>
  )
}
