import PlatformSelector from './PlatformSelector'
import BrandModeSelector from './BrandModeSelector'
import CandidateCountSelector from './CandidateCountSelector'
import PromptField from './PromptField'
import ImageModelSelector from './ImageModelSelector'
import type {
  Asset,
  BrandMode,
  BrandStyle,
  ImageModel,
  MerchantInfo,
  PlatformPreset,
  Ratio,
} from '../types'

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
  selectedRatio: Ratio
  merchantInfo: MerchantInfo
  products: Asset[]
  imageModel: ImageModel
  prompt: string
  candidateCount: number
  busy: boolean
  onBrandChange: (brandId: string) => void
  onBrandModeChange: (mode: BrandMode) => void
  onUploadStyleReferences: (files: File[]) => void
  onRemoveStyleReference: (assetId: string) => void
  onPlatformChange: (platformId: string) => void
  onPlacementChange: (placementId: string) => void
  onRatioChange: (ratio: Ratio) => void
  onMerchantChange: (merchant: MerchantInfo) => void
  onPickProducts: () => void
  onRemoveProduct: (id: string) => void
  onImageModelChange: (value: ImageModel) => void
  onPromptChange: (value: string, optimized?: boolean) => void
  onCandidateCountChange: (value: number) => void
  onGoAssets: () => void
  onGenerate: () => void
}

export default function WholeImageForm({
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
  selectedRatio,
  merchantInfo,
  products,
  imageModel,
  prompt,
  candidateCount,
  busy,
  onBrandChange,
  onBrandModeChange,
  onUploadStyleReferences,
  onRemoveStyleReference,
  onPlatformChange,
  onPlacementChange,
  onRatioChange,
  onMerchantChange,
  onPickProducts,
  onRemoveProduct,
  onImageModelChange,
  onPromptChange,
  onCandidateCountChange,
  onGoAssets,
  onGenerate,
}: Props) {
  const selectedPlatform = platforms.find((item) => item.id === selectedPlatformId)
  const brandReady =
    brandMode !== 'preset' ||
    (!brandsLoading &&
      !brandsError &&
      brands.some((brand) => brand.id === selectedBrandId))
  const optimizationReady =
    products.length > 0 &&
    brandReady &&
    (!selectedPlatformId || Boolean(selectedPlatform))
  const optimizationBlockedReason =
    products.length === 0
      ? '请先选择参考素材'
      : !brandReady
        ? '请先选择有效的品牌风格'
        : selectedPlatformId && !selectedPlatform
          ? '请先等待平台配置就绪'
          : undefined

  return (
    <section className="mp-card form-card">
      <div className="panel-head">
        <div>
          <span className="eyebrow">01 · 需求</span>
          <h1>自由设计一张完整海报</h1>
          <p className="panel-lead">整图模型直接完成构图、文字和视觉设计，不拆分图层。</p>
        </div>
      </div>

      <PlatformSelector
        platforms={platforms}
        selectedPlatformId={selectedPlatformId}
        selectedPlacementId={selectedPlacementId}
        selectedRatio={selectedRatio}
        merchantInfo={merchantInfo}
        loading={platformsLoading}
        error={platformsError}
        busy={busy}
        onPlatformChange={onPlatformChange}
        onPlacementChange={onPlacementChange}
        onRatioChange={onRatioChange}
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

      <ImageModelSelector
        value={imageModel}
        busy={busy}
        onChange={onImageModelChange}
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
        <span className="product-hint">参考图只作为视觉输入，最终排版由模型一次完成。</span>
      </div>

      <PromptField
        label="设计需求"
        availability="必填"
        placeholder="例如：夏日冰美式新品海报，年轻、有设计感，标题写「夏日限定」"
        hint="描述内容、风格、文案和限制；模型会直接输出完整设计。"
        prompt={prompt}
        busy={busy}
        canOptimize={optimizationReady}
        optimizationBlockedReason={optimizationBlockedReason}
        optimizeContext={{
          brand_mode: brandMode,
          brand_id: brandMode === 'preset' ? selectedBrandId : null,
          style_reference_asset_ids:
            brandMode === 'reference' ? styleReferences.map((asset) => asset.id) : [],
          product_asset_ids: products.map((asset) => asset.id),
          platform_id: selectedPlatform?.id ?? selectedPlatformId ?? null,
          placement_id: selectedPlacementId || null,
          merchant_info:
            selectedPlatform?.contact_policy !== 'forbidden' ? merchantInfo : null,
        }}
        onPromptChange={onPromptChange}
      />

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
          busy ||
          !prompt.trim() ||
          !brandReady ||
          products.length === 0 ||
          (Boolean(selectedPlatformId) && !selectedPlatform)
        }
      >
        {busy ? (
          <>
            <span className="spinner" aria-hidden="true" />
            生成中
          </>
        ) : (
          '生成设计'
        )}
      </button>
      <p className="form-footnote">
        首轮只调用一次图像模型。生成期间可离开页面，回来用链接继续查看。
      </p>
    </section>
  )
}
