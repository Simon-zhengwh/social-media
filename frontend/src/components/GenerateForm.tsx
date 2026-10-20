import type { Asset, BrandStyle, Ratio } from '../types'

const RATIO_OPTIONS: Array<{ value: Ratio; label: string; hint: string }> = [
  { value: '1:1', label: '1:1', hint: '方形' },
  { value: '9:16', label: '9:16', hint: '竖版' },
  { value: '4:5', label: '4:5', hint: '社媒' },
  { value: '16:9', label: '16:9', hint: '横版' },
]

type Props = {
  brands: BrandStyle[]
  selectedBrandId: string
  brandsLoading: boolean
  brandsError: string | null
  products: Asset[]
  logo: Asset | null
  ratio: Ratio
  prompt: string
  busy: boolean
  onBrandChange: (brandId: string) => void
  onPickProducts: () => void
  onRemoveProduct: (id: string) => void
  onPickLogo: () => void
  onClearLogo: () => void
  onRatioChange: (ratio: Ratio) => void
  onPromptChange: (value: string) => void
  onGoAssets: () => void
  onGenerate: () => void
}

export default function GenerateForm({
  brands,
  selectedBrandId,
  brandsLoading,
  brandsError,
  products,
  logo,
  ratio,
  prompt,
  busy,
  onBrandChange,
  onPickProducts,
  onRemoveProduct,
  onPickLogo,
  onClearLogo,
  onRatioChange,
  onPromptChange,
  onGoAssets,
  onGenerate,
}: Props) {
  return (
    <section className="mp-card form-card">
      <div className="panel-head">
        <div>
          <span className="eyebrow">01 · 素材</span>
          <h1>生成一张品牌海报</h1>
          <p className="panel-lead">从资产管理选择商品图与 Logo，AI 生成场景底图。</p>
        </div>
      </div>

      <div className="brand-field">
        <div className="brand-field-head">
          <span className="prompt-field-label">
            品牌风格 <span>必选</span>
          </span>
          <span className="brand-field-hint">品牌决定配色、字体、构图与 Logo 规则</span>
        </div>
        {brandsError ? (
          <div className="brand-empty brand-error" role="alert">
            <strong>品牌风格加载失败</strong>
            <span>{brandsError}</span>
          </div>
        ) : brandsLoading ? (
          <div className="brand-empty">
            <span className="brand-loading-dot" aria-hidden="true" />
            <span>正在读取品牌 skill</span>
          </div>
        ) : brands.length === 0 ? (
          <div className="brand-empty">
            <strong>没有可用的品牌风格</strong>
            <span>请检查项目中 .agents/skills 下的品牌 skill。</span>
          </div>
        ) : (
          <div className="brand-options" role="radiogroup" aria-label="品牌风格">
            {brands.map((brand) => {
              const selected = brand.id === selectedBrandId
              const swatches = Object.entries(brand.palette)
                .filter(([key]) =>
                  ['primary', 'accent', 'accent_bright', 'cream', 'highlight'].includes(key),
                )
                .slice(0, 5)
              return (
                <button
                  key={brand.id}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  className={`brand-option${selected ? ' is-active' : ''}`}
                  onClick={() => onBrandChange(brand.id)}
                  disabled={busy}
                >
                  <span className="brand-cover">
                    {brand.cover_url ? (
                      <img src={brand.cover_url} alt={`${brand.name} 风格参考`} />
                    ) : (
                      <span className="brand-cover-placeholder">{brand.short_name.slice(0, 2)}</span>
                    )}
                    {selected ? <span className="brand-selected-mark">当前</span> : null}
                  </span>
                  <span className="brand-option-copy">
                    <strong>{brand.name}</strong>
                    <span>{brand.summary || '品牌专属视觉规范'}</span>
                    <span className="brand-swatches" aria-hidden="true">
                      {swatches.map(([token, color]) => (
                        <span
                          key={token}
                          className="brand-swatch"
                          style={{ background: color }}
                          title={`${token} ${color}`}
                        />
                      ))}
                    </span>
                  </span>
                </button>
              )
            })}
          </div>
        )}
      </div>

      <div className="product-block">
        <div className="product-block-head">
          <strong>菜品 / 商品图</strong>
          <span>必填 · 最多 4 张</span>
        </div>
        {products.length > 0 ? (
          <div className="product-strip">
            {products.map((product, index) => (
              <div className="product-thumb" key={product.id}>
                <img
                  className={`product-thumb-img product-thumb-img-${product.kind}`}
                  src={product.url}
                  alt={product.original_name}
                />
                <span className="product-index">{index + 1}</span>
                <button
                  type="button"
                  className="product-remove"
                  onClick={() => onRemoveProduct(product.id)}
                  aria-label={`移除 ${product.original_name}`}
                >
                  ×
                </button>
              </div>
            ))}
            {products.length < 4 ? (
              <button type="button" className="product-add" onClick={onPickProducts}>
                <span className="upload-plus" aria-hidden="true" />
                <span>添加</span>
              </button>
            ) : null}
          </div>
        ) : (
          <button type="button" className="product-empty" onClick={onPickProducts}>
            <span className="upload-plus" aria-hidden="true" />
            <strong>从资产管理选择商品图</strong>
            <span>可多选，第一张作为主视觉</span>
          </button>
        )}
        <span className="product-hint">多张图会融合成同一张海报；第一张作为主视觉。</span>
      </div>

      <div className={`asset-slot${logo ? ' has-asset' : ''}`}>
        <div className="asset-slot-head">
          <strong>品牌 Logo</strong>
          <span className="asset-optional">可选</span>
        </div>
        {logo ? (
          <div className="asset-slot-body">
            <img
              className="asset-slot-thumb asset-slot-thumb-logo"
              src={logo.url}
              alt={logo.original_name}
            />
            <div className="asset-slot-meta">
              <strong title={logo.original_name}>{logo.original_name}</strong>
              <span className="tnum">
                {logo.width} × {logo.height}
              </span>
            </div>
          </div>
        ) : (
          <div className="asset-slot-empty">
            <span className="upload-plus" aria-hidden="true" />
            <span>可选择，也可以不添加</span>
          </div>
        )}
        <div className="asset-slot-actions">
          <button type="button" className="asset-slot-pick" onClick={onPickLogo}>
            {logo ? '更换' : '从资产管理选择'}
          </button>
          {logo ? (
            <button type="button" className="asset-slot-clear" onClick={onClearLogo}>
              移除
            </button>
          ) : null}
        </div>
      </div>

      <div className="ratio-field">
        <span className="prompt-field-label">画面比例</span>
        <div className="ratio-options">
          {RATIO_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              className={`ratio-option${ratio === option.value ? ' is-active' : ''}`}
              onClick={() => onRatioChange(option.value)}
              disabled={busy}
            >
              <strong>{option.label}</strong>
              <span>{option.hint}</span>
            </button>
          ))}
        </div>
      </div>

      <label className="prompt-field">
        <span className="prompt-field-label">
          提示词 <span>可空</span>
        </span>
        <textarea
          value={prompt}
          maxLength={2000}
          rows={5}
          placeholder="例如：用素材图的信息制作一张 9:16 IG 海报，深色背景、顶部聚光灯，文案清晰可读"
          onChange={(event) => onPromptChange(event.target.value)}
          disabled={busy}
        />
        <span className="prompt-field-hint">
          描述背景、光线、构图和文案；留空使用默认暖色餐厅场景。
        </span>
      </label>

      <button type="button" className="asset-library-link" onClick={onGoAssets}>
        去资产管理上传或载入示例素材
        <span aria-hidden="true">→</span>
      </button>

      <button
        type="button"
        className="mp-btn-primary mp-btn-block"
        onClick={onGenerate}
        disabled={
          products.length === 0 ||
          busy ||
          brandsLoading ||
          Boolean(brandsError) ||
          brands.length === 0 ||
          !brands.some((brand) => brand.id === selectedBrandId)
        }
      >
        {busy ? (
          <>
            <span className="spinner" aria-hidden="true" />
            生成中
          </>
        ) : (
          '生成海报'
        )}
      </button>
      <p className="form-footnote">ToAPIs 生图约需 1–3 分钟。生成期间可离开页面，回来用链接继续查看。</p>
    </section>
  )
}
