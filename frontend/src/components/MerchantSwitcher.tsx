import { useEffect, useMemo, useRef, useState } from 'react'

import { listMerchants, type MerchantOption } from '../api'

type Props = {
  /** The merchant the workspace is currently scoped to. */
  currentId: string
  currentName: string
}

/**
 * Switch which merchant the workspace works for.
 *
 * Switching is a navigation, not an in-place state swap: every panel below the topbar reads the
 * merchant from the URL, so a reload is what guarantees no task, asset or brand from the previous
 * merchant survives the switch. Only the merchant id changes; the current view is kept so the
 * operator lands back where they were.
 */
export default function MerchantSwitcher({ currentId, currentName }: Props) {
  const [open, setOpen] = useState(false)
  const [merchants, setMerchants] = useState<MerchantOption[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [keyword, setKeyword] = useState('')
  const rootRef = useRef<HTMLDivElement>(null)
  const requestId = useRef(0)

  const load = (search: string) => {
    const id = requestId.current + 1
    requestId.current = id
    setLoading(true)
    setError(null)
    listMerchants(search)
      .then((items) => {
        if (requestId.current === id) setMerchants(items)
      })
      .catch((reason: unknown) => {
        if (requestId.current !== id) return
        setMerchants([])
        setError(reason instanceof Error ? reason.message : '加载商户列表失败')
      })
      .finally(() => {
        if (requestId.current === id) setLoading(false)
      })
  }

  const toggle = () => {
    const next = !open
    setOpen(next)
    if (next && merchants.length === 0) load(keyword)
  }

  const onKeyword = (value: string) => {
    setKeyword(value)
    load(value)
  }

  useEffect(() => {
    if (!open) return
    const close = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false)
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', escape)
    }
  }, [open])

  const filtered = useMemo(() => {
    const needle = keyword.trim().toLowerCase()
    if (!needle) return merchants
    return merchants.filter((item) => item.name.toLowerCase().includes(needle))
  }, [merchants, keyword])

  const choose = (merchant: MerchantOption) => {
    if (merchant.id === currentId) {
      setOpen(false)
      return
    }
    const url = new URL(window.location.href)
    url.searchParams.set('merchant_id', merchant.id)
    // A full navigation, so the next merchant's workspace cannot inherit this one's state.
    window.location.assign(url.toString())
  }

  return (
    <div className="merchant-switcher" ref={rootRef}>
      <button
        type="button"
        className="merchant-switcher-trigger"
        onClick={toggle}
        aria-haspopup="listbox"
        aria-expanded={open}
        title="切换商户"
      >
        <span className="status-dot status-dot-live" aria-hidden="true" />
        <span className="merchant-switcher-name">{currentName || '商家工作区'}</span>
        <span className="merchant-switcher-caret" aria-hidden="true">▾</span>
      </button>
      {open ? (
        <div className="merchant-switcher-panel" role="listbox" aria-label="选择商户">
          <input
            className="merchant-switcher-search"
            placeholder="搜索商户"
            value={keyword}
            onChange={(event) => onKeyword(event.target.value)}
            autoFocus
          />
          {error ? <p className="merchant-switcher-empty is-error">{error}</p> : null}
          {loading && merchants.length === 0 ? (
            <p className="merchant-switcher-empty">正在加载…</p>
          ) : null}
          {!loading && !error && filtered.length === 0 ? (
            <p className="merchant-switcher-empty">没有匹配的商户</p>
          ) : null}
          <ul className="merchant-switcher-list">
            {filtered.map((merchant) => (
              <li key={merchant.id}>
                <button
                  type="button"
                  role="option"
                  aria-selected={merchant.id === currentId}
                  className={'merchant-switcher-option' + (merchant.id === currentId ? ' is-current' : '')}
                  onClick={() => choose(merchant)}
                >
                  <span>{merchant.name}</span>
                  {merchant.id === currentId ? <span className="merchant-switcher-check">当前</span> : null}
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  )
}
