import type { MerchantProfile } from '../types'

export default function MerchantProfilePanel({ profile }: { profile: MerchantProfile | null }) {
  return (
    <aside className="brand-form-section merchant-profile-panel" aria-label="商户特点">
      <div className="brand-section-head"><div><span className="eyebrow">MERCHANT PROFILE</span><h2>商户特点</h2></div><span className="merchant-profile-badge">商户共用</span></div>
      {profile ? <>
        <p className="merchant-profile-source">{profile.source_label}。以下为资料摘录及中文整理，未独立核验。</p>
        <dl className="merchant-profile-fields">
          {profile.sections.map((section, index) => <div className={index === 0 ? 'merchant-profile-intro' : ''} key={section.source_path}>
            <dt>{section.title.replace(/（.*）$/u, '')}</dt><dd>{section.text || '资料未提供'}</dd>
          </div>)}
        </dl>
        <details className="merchant-profile-provenance"><summary>查看原始资料与字段来源</summary>
          <p>资料记录：{profile.source_record_id}</p>
          {profile.sections.map(section => <div key={section.source_path}><strong>{section.title}</strong><code>{section.source_path}</code><pre>{typeof section.original === 'string' ? section.original : JSON.stringify(section.original, null, 2)}</pre></div>)}
        </details>
      </> : <div className="merchant-profile-empty"><strong>商户资料待补充</strong><p>尚未关联商户资料。补充定位、核心卖点、客群和品牌描述后在这里展示。</p></div>}
    </aside>
  )
}
