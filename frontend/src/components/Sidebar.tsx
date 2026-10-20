import type { ReactElement } from 'react'

export type AppView = 'poster' | 'assets'

type Props = {
  view: AppView
  onChange: (view: AppView) => void
}

function PosterIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <rect x="2.8" y="3.8" width="14.4" height="12.4" rx="2.2" />
      <circle cx="12.8" cy="7.8" r="1.2" />
      <path d="M4.8 13.5l3.1-3.1 2.1 2.1 1.7-1.7 3.5 3.5" />
    </svg>
  )
}

function AssetsIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <rect x="3.2" y="5.2" width="11.6" height="11.3" rx="2" />
      <path d="M6.6 3.2h8.2a2 2 0 0 1 2 2v8.2" />
      <path d="M5.8 13.2l2.5-2.5 1.8 1.8 1.2-1.2 2.2 2.2" />
    </svg>
  )
}

const NAV_ITEMS: Array<{
  key: AppView
  label: string
  hint: string
  icon: () => ReactElement
}> = [
  {
    key: 'poster',
    label: '海报生成',
    hint: '从素材库选择并生成',
    icon: PosterIcon,
  },
  {
    key: 'assets',
    label: '资产管理',
    hint: '商品图与 Logo',
    icon: AssetsIcon,
  },
]

export default function Sidebar({ view, onChange }: Props) {
  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        <svg className="brand-mark" viewBox="0 0 40 40" aria-hidden="true">
          <circle
            cx="20"
            cy="20"
            r="15"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.4"
            strokeDasharray="68 26"
            strokeLinecap="round"
          />
          <circle cx="20" cy="20" r="4.2" fill="currentColor" />
        </svg>
        <span className="sidebar-brand-copy">
          <strong>朵颐视觉工作台</strong>
          <span>SICHUAN GOURMET</span>
        </span>
      </div>

      <nav className="sidebar-nav" aria-label="主导航">
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon
          return (
            <button
              key={item.key}
              type="button"
              className={`nav-item${view === item.key ? ' is-active' : ''}`}
              onClick={() => onChange(item.key)}
              aria-current={view === item.key ? 'page' : undefined}
              aria-label={item.label}
              title={item.label}
            >
              <span className="nav-icon">
                <Icon />
              </span>
              <span className="nav-copy">
                <strong>{item.label}</strong>
                <span>{item.hint}</span>
              </span>
            </button>
          )
        })}
      </nav>

      <div className="sidebar-foot">
        <span className="sidebar-chip">分层</span>
        <span>文字 · Logo · 版本</span>
        <span className="sidebar-sep" aria-hidden="true" />
        <span>ToAPIs GPT-Image-2.5</span>
      </div>
    </aside>
  )
}
