import type { BrandLogoImage } from '../types'
import { ZoomableImage } from './ImageLightbox'

type Props = {
  /** The merchant-level logos configured on the brand page; never ordinary content assets. */
  logos: BrandLogoImage[]
  selectedLogoId: string
  busy: boolean
  /** Picks which logo this poster uses. Only reachable while the logo switch is on. */
  onSelectLogo: (assetId: string) => void
  /**
   * Whether this poster carries a logo at all. The switch is the on/off control; the tiles below
   * only decide which logo, so turning it off must remember nothing but the merchant's own set.
   */
  onToggleLogo: (enabled: boolean) => void
}

export default function LogoSelector({ logos, selectedLogoId, busy, onSelectLogo, onToggleLogo }: Props) {
  const enabled = Boolean(selectedLogoId)
  // With a single logo there is nothing to pick: keep the switch semantics, drop the selection
  // highlight and the radio marker so the only option does not look like an unanswered choice.
  const selectable = logos.length > 1
  return (
    <div className="brand-field logo-selector-field">
      <div className="field-switch-row">
        <span className="field-switch-caption"><strong>Logo</strong></span>
        <label className="field-switch">
          <input
            type="checkbox"
            role="switch"
            aria-label="在海报中加入 Logo"
            checked={enabled}
            disabled={busy || logos.length === 0}
            onChange={(event) => onToggleLogo(event.target.checked)}
          />
          <span aria-hidden="true" />
        </label>
      </div>

      {logos.length === 0 ? (
        <p className="logo-selector-empty">
          暂无 Logo，可在「品牌风格」中添加。
        </p>
      ) : !enabled ? null : (
        <div className={`logo-selector-options${selectable ? '' : ' is-single'}`} aria-label="品牌 Logo">
          {logos.map((logo) => {
            const active = logo.asset_id === selectedLogoId
            const highlighted = active && selectable
            const name = logo.original_name || '品牌 Logo'
            return (
              <div
                key={logo.asset_id}
                className={`logo-selector-option${highlighted ? ' is-active' : ''}`}
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
                  onClick={() => onSelectLogo(logo.asset_id)}
                  title={`本张海报使用 ${name}`}
                >
                  <span className="logo-selector-radio" aria-hidden="true" />
                  <span className="logo-selector-name">{name}</span>
                </button>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
