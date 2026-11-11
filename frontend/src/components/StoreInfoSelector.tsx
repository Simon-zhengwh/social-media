import type { MerchantStore } from '../types'

export type StoreSelection = {
  enabled: boolean
  placeId: string
  address: boolean
  phone: boolean
}

type Props = {
  stores: MerchantStore[]
  value: StoreSelection
  disabled: boolean
  onChange: (next: StoreSelection) => void
}

/**
 * Whether the poster may print the store address and phone.
 *
 * Address and phone are separate switches because a merchant may allow one and not the other.
 * The store matters too: the same brand has different addresses per location, so the operator
 * picks which one the poster is for rather than the code guessing.
 */
export default function StoreInfoSelector({ stores, value, disabled, onChange }: Props) {
  if (!stores.length) return null

  const selected = stores.find((store) => store.place_id === value.placeId) ?? stores[0]
  const hasAddress = Boolean(selected?.address)
  const hasPhone = Boolean(selected?.phone)

  const patch = (part: Partial<StoreSelection>) => onChange({ ...value, ...part })

  return (
    <div className="store-info-block">
      <div className="field-switch-row">
        <span className="field-switch-caption"><strong>门店信息</strong></span>
        <label className="field-switch">
          <input
            type="checkbox"
            role="switch"
            aria-label="在海报中显示门店信息"
            checked={value.enabled}
            disabled={disabled}
            onChange={(event) => patch({ enabled: event.target.checked })}
          />
          <span aria-hidden="true" />
        </label>
      </div>

      {value.enabled ? (
        <>
          <label className="store-info-field">
            <span>门店</span>
            <select
              value={selected?.place_id ?? ''}
              disabled={disabled}
              onChange={(event) => patch({ placeId: event.target.value })}
            >
              {stores.map((store) => (
                <option key={store.place_id} value={store.place_id}>
                  {store.name || store.place_id}
                </option>
              ))}
            </select>
          </label>

          <div className="store-info-checks" role="group" aria-label="选择要写进海报的信息">
            <label className={hasAddress ? '' : 'is-unavailable'}>
              <input
                type="checkbox"
                checked={value.address && hasAddress}
                disabled={disabled || !hasAddress}
                onChange={(event) => patch({ address: event.target.checked })}
              />
              <span>地址{hasAddress ? ` · ${selected.address}` : '（资料未提供）'}</span>
            </label>
            <label className={hasPhone ? '' : 'is-unavailable'}>
              <input
                type="checkbox"
                checked={value.phone && hasPhone}
                disabled={disabled || !hasPhone}
                onChange={(event) => patch({ phone: event.target.checked })}
              />
              <span>电话{hasPhone ? ` · ${selected.phone}` : '（资料未提供）'}</span>
            </label>
          </div>
        </>
      ) : null}
    </div>
  )
}
