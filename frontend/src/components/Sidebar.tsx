import type { ReactElement } from 'react'

export type AppView = 'poster' | 'whole' | 'assets' | 'brands'

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

function WholeImageIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <rect x="2.8" y="3.2" width="14.4" height="13.6" rx="2.2" />
      <path d="M5.2 13.8l3.2-3.4 2.4 2.3 1.7-1.8 2.5 2.7" />
      <path d="M6.2 6.4h7.6M6.2 8.8h4.8" />
    </svg>
  )
}

function BrandsIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <path d="M3.4 6.3h13.2v9.5H3.4z" />
      <path d="M6.2 6.3V4.5h7.6v1.8" />
      <path d="M6.2 10.1h7.6M6.2 12.9h4.8" />
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
    key: 'whole',
    label: '自由设计',
    hint: '模型直出整图 · 不拆图层',
    icon: WholeImageIcon,
  },
  {
    key: 'assets',
    label: '资产管理',
    hint: '图片素材与参考图',
    icon: AssetsIcon,
  },
  {
    key: 'brands',
    label: '品牌风格',
    hint: '规范、色板与参考图',
    icon: BrandsIcon,
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
        <span className="sidebar-chip">分层 / 整图</span>
        <span>两种工作流可自由切换</span>
        <span className="sidebar-sep" aria-hidden="true" />
        <span>core-ai GPT-Image-2.5</span>
      </div>
    </aside>
  )
}
