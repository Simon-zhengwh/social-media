import type { MerchantProfile } from '../types'

export default function MerchantProfilePanel({ profile }: { profile: MerchantProfile | null }) {
  return (
    <aside className="brand-form-section merchant-profile-panel" aria-label="商户特点">
      <div className="brand-section-head"><div><span className="eyebrow">MERCHANT PROFILE</span><h2>商户特点</h2></div><span className="merchant-profile-badge">AM 只读</span></div>
      {profile ? <>
        <p className="merchant-profile-source">{profile.source_label}。资料实时读取，仅作展示，不自动加入生成提示词。</p>
        <dl className="merchant-profile-fields">
          {profile.sections.map((section, index) => <div className={index === 0 ? 'merchant-profile-intro' : ''} key={section.source_path}>
            <dt>{section.title.replace(/（.*）$/u, '')}</dt><dd>{section.text || '资料未提供'}</dd>
          </div>)}
        </dl>
        <details className="merchant-profile-provenance"><summary>查看原始资料与字段来源</summary>
          <p>资料记录：{profile.source_record_id}</p>
          {profile.sections.map(section => <div key={section.source_path}><strong>{section.title}</strong><code>{section.source_path}</code><pre>{typeof section.original === 'string' ? section.original : JSON.stringify(section.original, null, 2)}</pre></div>)}
        </details>
      </> : <div className="merchant-profile-empty"><strong>商户资料待补充</strong><p>AM merchant-profile 暂无资料。请先在 AM Console 补充，并保持商户资料唯一来源。</p></div>}
    </aside>
  )
}
