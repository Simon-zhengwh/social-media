import type { MerchantProfile } from '../types'

export default function MerchantProfilePanel({ profile }: { profile: MerchantProfile | null }) {
  if (!profile?.prompt_sections?.length) return null
  return (
    <aside className="brand-form-section merchant-profile-panel" aria-label="商户特点">
      <div className="brand-section-head"><div><span className="eyebrow">MERCHANT PROFILE</span><h2>商户特点</h2></div><span className="merchant-profile-badge">用于提示词优化</span></div>
      <p className="merchant-profile-source">以下商户背景来自 AM 商户资料，会用于提示词优化，按本次要求选取相关信息。</p>
      <dl className="merchant-profile-fields">
        {profile.prompt_sections.map((section, index) => <div className={index === 0 ? 'merchant-profile-intro' : ''} key={section.title}>
          <dt>{section.title}</dt><dd>{section.text}</dd>
        </div>)}
      </dl>
    </aside>
  )
}
