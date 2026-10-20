import PlatformSelector from './PlatformSelector'
import BrandModeSelector from './BrandModeSelector'
import CandidateCountSelector from './CandidateCountSelector'
import type { Asset, BrandMode, BrandStyle, MerchantInfo, PlatformPreset } from '../types'

type Props = {
  brands: BrandStyle[]
  selectedBrandId: string
  brandsLoading: boolean
  brandsError: string | null
  brandMode: BrandMode
  styleReferences: Asset[]
  styleReferencesUploading: boolean
  platforms: PlatformPreset[]
  platformsLoading: boolean
  platformsError: string | null
  selectedPlatformId: string
  selectedPlacementId: string
  merchantInfo: MerchantInfo
  products: Asset[]
  prompt: string
  candidateCount: number
  busy: boolean
  onBrandChange: (brandId: string) => void
  onBrandModeChange: (mode: BrandMode) => void
  onUploadStyleReferences: (files: File[]) => void
  onRemoveStyleReference: (assetId: string) => void
  onPlatformChange: (platformId: string) => void
  onPlacementChange: (placementId: string) => void
  onMerchantChange: (merchant: MerchantInfo) => void
  onPickProducts: () => void
  onRemoveProduct: (id: string) => void
  onPromptChange: (value: string) => void
  onCandidateCountChange: (value: number) => void
  onGoAssets: () => void
  onGenerate: () => void
}

export default function GenerateForm({
  brands,
  selectedBrandId,
  brandsLoading,
  brandsError,
  brandMode,
  styleReferences,
  styleReferencesUploading,
  platforms,
  platformsLoading,
  platformsError,
  selectedPlatformId,
  selectedPlacementId,
  merchantInfo,
  products,
  prompt,
  candidateCount,
  busy,
  onBrandChange,
  onBrandModeChange,
  onUploadStyleReferences,
  onRemoveStyleReference,
  onPlatformChange,
  onPlacementChange,
  onMerchantChange,
  onPickProducts,
  onRemoveProduct,
  onPromptChange,
  onCandidateCountChange,
  onGoAssets,
  onGenerate,
}: Props) {
  const selectedPlatform = platforms.find((item) => item.id === selectedPlatformId)
  const contactReady =
    selectedPlatform?.contact_policy !== 'required' ||
    Boolean(merchantInfo.address.trim() && merchantInfo.phone.trim())
  const brandReady =
    brandMode !== 'preset' ||
    (!brandsLoading &&
      !brandsError &&
      brands.some((brand) => brand.id === selectedBrandId))

  return (
    <section className="mp-card form-card">
      <div className="panel-head">
        <div>
          <span className="eyebrow">01 · 素材</span>
          <h1>生成一张海报</h1>
          <p className="panel-lead">从资产管理选择参考素材，AI 生成场景底图。</p>
        </div>
      </div>

      <PlatformSelector
        platforms={platforms}
        selectedPlatformId={selectedPlatformId}
        selectedPlacementId={selectedPlacementId}
        merchantInfo={merchantInfo}
        loading={platformsLoading}
        error={platformsError}
        busy={busy}
        onPlatformChange={onPlatformChange}
        onPlacementChange={onPlacementChange}
        onMerchantChange={onMerchantChange}
      />

      <BrandModeSelector
        brands={brands}
        selectedBrandId={selectedBrandId}
        brandsLoading={brandsLoading}
        brandsError={brandsError}
        mode={brandMode}
        references={styleReferences}
        referencesUploading={styleReferencesUploading}
        busy={busy}
        onModeChange={onBrandModeChange}
        onBrandChange={onBrandChange}
        onUploadReferences={onUploadStyleReferences}
        onRemoveReference={onRemoveStyleReference}
      />

      <div className="product-block">
        <div className="product-block-head">
          <strong>参考素材</strong>
          <span>必选 · 最多 4 张</span>
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
            <strong>从资产管理选择参考素材</strong>
            <span>可多选</span>
          </button>
        )}
      </div>

      <label className="prompt-field">
        <span className="prompt-field-label">
          提示词 <span>可空</span>
        </span>
        <textarea
          value={prompt}
          maxLength={2000}
          rows={5}
          placeholder="例如：用素材图的信息制作一张新品海报，深色背景、顶部聚光灯，文案清晰可读"
          onChange={(event) => onPromptChange(event.target.value)}
          disabled={busy}
        />
        <span className="prompt-field-hint">
          描述背景、光线、构图和文案；留空使用默认暖色餐厅场景。
        </span>
      </label>

      <CandidateCountSelector
        value={candidateCount}
        busy={busy}
        onChange={onCandidateCountChange}
      />

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
          !brandReady ||
          platformsLoading ||
          Boolean(platformsError) ||
          !selectedPlatform ||
          !contactReady
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
      <p className="form-footnote">core-ai 生图约需 1–3 分钟。生成期间可离开页面，回来用链接继续查看。</p>
    </section>
  )
}
