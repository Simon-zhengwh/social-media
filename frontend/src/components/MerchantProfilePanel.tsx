import type { MerchantBrandInfo, MerchantProfile, MerchantStore } from '../types'

const EMPTY = '资料未提供'

function storeLine(store: MerchantStore): string {
  const parts = [store.address, store.suite].filter(Boolean)
  return parts.join(' ') || EMPTY
}

/**
 * Brand facts the operator needs to see but the model must not be told.
 *
 * The year is brand-wide; address and phone belong to each store, so a multi-store merchant
 * lists every store instead of pretending the first one speaks for the brand.
 */
function BrandInfoCard({ info }: { info: MerchantBrandInfo }) {
  const year = info.year_established
  const stores = info.stores ?? []
  return (
    <section className="merchant-info-card" aria-label="品牌信息">
      <div className="merchant-info-head">
        <span className="eyebrow">BRAND INFO</span>
        <h3>品牌信息</h3>
      </div>
      <dl className="merchant-info-list">
        <div>
          <dt>开业时间</dt>
          <dd>{year ? `${year}年` : EMPTY}</dd>
        </div>
      </dl>
      {stores.length ? (
        <div className="merchant-info-stores">
          <span className="merchant-info-stores-title">门店（{stores.length}）</span>
          {stores.map((store) => (
            <div className="merchant-info-store" key={store.place_id || store.name}>
              <strong>{store.name || EMPTY}</strong>
              <span>{storeLine(store)}</span>
              <span className="merchant-info-phone">{store.phone || EMPTY}</span>
            </div>
          ))}
        </div>
      ) : (
        <p className="merchant-info-empty">地址与电话：{EMPTY}</p>
      )}
    </section>
  )
}

export default function MerchantProfilePanel({ profile }: { profile: MerchantProfile | null }) {
  if (!profile) return null
  const hasPrompt = Boolean(profile.prompt_sections?.length)
  const info = profile.brand_info
  if (!hasPrompt && !info) return null
  return (
    <aside className="brand-form-section merchant-profile-panel" aria-label="商户特点">
      {hasPrompt ? (
        <>
          <div className="brand-section-head">
            <div>
              <span className="eyebrow">MERCHANT PROFILE</span>
              <h2>商户特点</h2>
            </div>
            <span className="merchant-profile-badge">用于提示词优化</span>
          </div>
          <p className="merchant-profile-source">以下商户背景来自 AM 商户资料，会用于提示词优化，按本次要求选取相关信息。</p>
          <dl className="merchant-profile-fields">
            {profile.prompt_sections.map((section, index) => (
              <div className={index === 0 ? 'merchant-profile-intro' : ''} key={section.title}>
                <dt>{section.title}</dt>
                <dd>{section.text}</dd>
              </div>
            ))}
          </dl>
        </>
      ) : null}
      {info ? <BrandInfoCard info={info} /> : null}
    </aside>
  )
}
