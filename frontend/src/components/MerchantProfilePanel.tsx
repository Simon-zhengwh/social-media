import type { ReactNode } from 'react'

import type { MerchantBrandInfo, MerchantProfile, MerchantStore } from '../types'

const EMPTY = '资料未提供'

function storeLine(store: MerchantStore): string {
  const parts = [store.address, store.suite].filter(Boolean)
  return parts.join(' ') || EMPTY
}

/**
 * Brand facts the operator needs to see but the model must not be told.
 *
 * Address and phone belong to each store, so a multi-store merchant lists every store instead of
 * letting the first one speak for the brand. Rendered as its own card above the profile because
 * it is the first thing a reader checks, not an appendix. The shared logo set sits in the same
 * card as the first slot, since both are merchant-level identity, not style configuration.
 */
export function MerchantBrandInfoCard({ info, logo }: { info: MerchantBrandInfo; logo?: ReactNode }) {
  const stores = info.stores ?? []
  return (
    <section className="brand-form-section merchant-info-card" aria-label="品牌信息">
      <div className="brand-section-head">
        <div>
          <span className="eyebrow">BRAND INFO</span>
          <h2>品牌信息</h2>
        </div>
      </div>
      {logo ? <div className="merchant-info-logo">{logo}</div> : null}
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
  if (!profile?.prompt_sections?.length) return null
  return (
    <aside className="brand-form-section merchant-profile-panel" aria-label="商户特点">
      <div className="brand-section-head">
        <div>
          <span className="eyebrow">MERCHANT PROFILE</span>
          <h2>商户特点</h2>
        </div>
        <span className="merchant-profile-badge">用于提示词优化</span>
      </div>
      <p className="merchant-profile-source">以下商户背景来自 AM商户资料，会用于提示词优化，按本次要求选取相关信息。</p>
      <dl className="merchant-profile-fields">
        {profile.prompt_sections.map((section, index) => (
          <div className={index === 0 ? 'merchant-profile-intro' : ''} key={section.title}>
            <dt>{section.title}</dt>
            <dd>{section.text}</dd>
          </div>
        ))}
      </dl>
    </aside>
  )
}
