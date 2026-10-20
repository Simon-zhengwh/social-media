import type { MerchantInfo, PlatformPreset } from '../types'

type Props = {
  platforms: PlatformPreset[]
  selectedPlatformId: string
  selectedPlacementId: string
  merchantInfo: MerchantInfo
  loading: boolean
  error: string | null
  busy: boolean
  onPlatformChange: (platformId: string) => void
  onPlacementChange: (placementId: string) => void
  onMerchantChange: (merchant: MerchantInfo) => void
}

export default function PlatformSelector({
  platforms,
  selectedPlatformId,
  selectedPlacementId,
  merchantInfo,
  loading,
  error,
  busy,
  onPlatformChange,
  onPlacementChange,
  onMerchantChange,
}: Props) {
  const selectedPlatform = platforms.find((item) => item.id === selectedPlatformId) ?? null
  const selectedPlacement =
    selectedPlatform?.placements.find((item) => item.id === selectedPlacementId) ?? null

  return (
    <div className="platform-field">
      <div className="brand-field-head">
        <span className="prompt-field-label">
          发布平台 <span>必选</span>
        </span>
        <span className="brand-field-hint">平台与用途决定尺寸和安全区</span>
      </div>

      {error ? (
        <div className="brand-empty brand-error" role="alert">
          <strong>平台规则加载失败</strong>
          <span>{error}</span>
        </div>
      ) : loading ? (
        <div className="brand-empty">
          <span className="brand-loading-dot" aria-hidden="true" />
          <span>正在读取平台预设</span>
        </div>
      ) : (
        <>
          <label className="platform-select-field">
            <span>选择平台</span>
            <select
              value={selectedPlatformId}
              onChange={(event) => onPlatformChange(event.target.value)}
              disabled={busy}
              aria-label="选择发布平台"
            >
              {platforms.map((platform) => (
                <option key={platform.id} value={platform.id}>
                  {platform.name}
                </option>
              ))}
            </select>
            {selectedPlatform ? (
              <small>{selectedPlatform.summary}</small>
            ) : null}
          </label>

          {selectedPlatform ? (
            <div className="placement-field">
              <span className="platform-sub-label">发布用途</span>
              <div className="placement-options" role="radiogroup" aria-label="发布用途">
                {selectedPlatform.placements.map((placement) => (
                  <button
                    key={placement.id}
                    type="button"
                    role="radio"
                    aria-checked={selectedPlacement?.id === placement.id}
                    className={`placement-option${selectedPlacement?.id === placement.id ? ' is-active' : ''}`}
                    onClick={() => onPlacementChange(placement.id)}
                    disabled={busy}
                  >
                    <strong>{placement.name}</strong>
                    <span>{placement.ratio}</span>
                  </button>
                ))}
              </div>
            </div>
          ) : null}

          {selectedPlatform?.contact_policy === 'required' ? (
            <div className="merchant-fields">
              <label>
                <span>商家地址 <em>必填</em></span>
                <input
                  value={merchantInfo.address}
                  maxLength={240}
                  placeholder="例如：上海市徐汇区某路 88 号"
                  onChange={(event) =>
                    onMerchantChange({ ...merchantInfo, address: event.target.value })
                  }
                  disabled={busy}
                />
              </label>
              <label>
                <span>联系电话 <em>必填</em></span>
                <input
                  value={merchantInfo.phone}
                  maxLength={80}
                  placeholder="例如：021-1234 5678"
                  onChange={(event) =>
                    onMerchantChange({ ...merchantInfo, phone: event.target.value })
                  }
                  disabled={busy}
                />
              </label>
              <p className="platform-contact-note">
                地址和电话会作为成图内容展示，生成前请核对真实信息。
              </p>
            </div>
          ) : null}
        </>
      )}
    </div>
  )
}
