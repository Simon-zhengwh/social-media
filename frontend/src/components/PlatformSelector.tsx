import type { MerchantInfo, PlatformPreset, Ratio } from '../types'

const FREE_RATIOS: Array<{ value: Ratio; label: string }> = [
  { value: '1:1', label: '方形' },
  { value: '4:5', label: '竖版' },
  { value: '3:4', label: '图文' },
  { value: '9:16', label: '长竖版' },
  { value: '4:3', label: '横版' },
  { value: '16:9', label: '宽屏' },
]

type Props = {
  platforms: PlatformPreset[]
  selectedPlatformId: string
  selectedPlacementId: string
  selectedRatio: Ratio
  merchantInfo: MerchantInfo
  loading: boolean
  error: string | null
  busy: boolean
  onPlatformChange: (platformId: string) => void
  onPlacementChange: (placementId: string) => void
  onRatioChange: (ratio: Ratio) => void
  onMerchantChange: (merchant: MerchantInfo) => void
}

export default function PlatformSelector({
  platforms,
  selectedPlatformId,
  selectedPlacementId,
  selectedRatio,
  merchantInfo,
  loading,
  error,
  busy,
  onPlatformChange,
  onPlacementChange,
  onRatioChange,
  onMerchantChange,
}: Props) {
  const selectedPlatform = platforms.find((item) => item.id === selectedPlatformId) ?? null
  const selectedPlacement =
    selectedPlatform?.placements.find((item) => item.id === selectedPlacementId) ?? null

  return (
    <div className="platform-field">
      <div className="brand-field-head">
        <span className="prompt-field-label">
          发布平台 <span>选填</span>
        </span>
        <span className="brand-field-hint">不选则由 AI 自由发挥</span>
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
              <option value="">不指定 · 由 AI 自由发挥</option>
              {platforms.map((platform) => (
                <option key={platform.id} value={platform.id}>
                  {platform.name}
                </option>
              ))}
            </select>
            <small>
              {selectedPlatform
                ? selectedPlatform.summary
                : '不套用特定平台规则，由 Agent 根据主题和素材自主决定构图与视觉方向。'}
            </small>
          </label>

          {!selectedPlatform ? (
            <div className="ratio-field">
              <span className="platform-sub-label">画幅比例</span>
              <div className="ratio-options" role="radiogroup" aria-label="画幅比例">
                {FREE_RATIOS.map((ratio) => (
                  <button
                    key={ratio.value}
                    type="button"
                    role="radio"
                    aria-checked={selectedRatio === ratio.value}
                    className={`ratio-option${selectedRatio === ratio.value ? ' is-active' : ''}`}
                    onClick={() => onRatioChange(ratio.value)}
                    disabled={busy}
                  >
                    <strong>{ratio.value}</strong>
                    <span>{ratio.label}</span>
                  </button>
                ))}
              </div>
              <p className="platform-contact-note">
                未选择平台时使用此比例；选择平台后由发布用途决定。
              </p>
            </div>
          ) : null}

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

          {selectedPlatform && selectedPlatform.contact_policy !== 'forbidden' ? (
            <div className="merchant-fields">
              <label>
                <span>商家地址 <em>选填</em></span>
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
                <span>联系电话 <em>选填</em></span>
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
                填写后会作为成图内容展示，请核对真实信息；不填写也可直接生成。
              </p>
            </div>
          ) : null}
        </>
      )}
    </div>
  )
}
