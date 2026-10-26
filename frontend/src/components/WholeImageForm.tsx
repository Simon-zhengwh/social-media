import PlatformSelector from './PlatformSelector'
import BrandModeSelector from './BrandModeSelector'
import CandidateCountSelector from './CandidateCountSelector'
import PromptField from './PromptField'
import LogoSelector from './LogoSelector'
import { ZoomableImage } from './ImageLightbox'
import type {
  Asset,
  BrandMode,
  BrandStyle,
  BrandLogoImage,
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
  logoOptions: BrandLogoImage[]
  selectedLogoId: string
  logoUploading: boolean
  logoDeleting: boolean
  canAddLogo: boolean
  platforms: PlatformPreset[]
  platformsLoading: boolean
  platformsError: string | null
  selectedPlatformId: string
  selectedPlacementId: string
  selectedRatio: Ratio
  products: Asset[]
  prompt: string
  promptOptimized: boolean
  candidateCount: number
  busy: boolean
  onBrandChange: (brandId: string) => void
  onBrandModeChange: (mode: BrandMode) => void
  onUploadStyleReferences: (files: File[]) => void
  onRemoveStyleReference: (assetId: string) => void
  onUploadLogos: (files: File[]) => void
  onPickLogo: () => void
  onRemoveLogo: (assetId: string) => void
  onPlatformChange: (platformId: string) => void
  onPlacementChange: (placementId: string) => void
  onRatioChange: (ratio: Ratio) => void
  onPickProducts: () => void
  onRemoveProduct: (id: string) => void
  onPromptChange: (value: string, optimized?: boolean) => void
  onCandidateCountChange: (value: number) => void
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
  logoOptions,
  selectedLogoId,
  logoUploading,
  logoDeleting,
  canAddLogo,
  platforms,
  platformsLoading,
  platformsError,
  selectedPlatformId,
  selectedPlacementId,
  selectedRatio,
  products,
  prompt,
  promptOptimized,
  candidateCount,
  busy,
  onBrandChange,
  onBrandModeChange,
  onUploadStyleReferences,
  onRemoveStyleReference,
  onUploadLogos,
  onPickLogo,
  onRemoveLogo,
  onPlatformChange,
  onPlacementChange,
  onRatioChange,
  onPickProducts,
  onRemoveProduct,
  onPromptChange,
  onCandidateCountChange,
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
      ? '请先选择素材图'
      : !brandReady
        ? '请先选择有效的品牌风格'
        : selectedPlatformId && !selectedPlatform
          ? '请先等待平台配置就绪'
          : undefined

  return (
    <section className="mp-card form-card">
      <PlatformSelector
        platforms={platforms}
        selectedPlatformId={selectedPlatformId}
        selectedPlacementId={selectedPlacementId}
        selectedRatio={selectedRatio}
        loading={platformsLoading}
        error={platformsError}
        busy={busy}
        onPlatformChange={onPlatformChange}
        onPlacementChange={onPlacementChange}
        onRatioChange={onRatioChange}
      />

      <BrandModeSelector
        brands={brands}
        selectedBrandId={selectedBrandId}
        brandsLoading={brandsLoading}
        brandsError={brandsError}
        platformRequired={!selectedPlatformId}
        mode={brandMode}
        references={styleReferences}
        referencesUploading={styleReferencesUploading}
        busy={busy}
        onModeChange={onBrandModeChange}
        onBrandChange={onBrandChange}
        onUploadReferences={onUploadStyleReferences}
        onRemoveReference={onRemoveStyleReference}
      />

      <LogoSelector
        logos={logoOptions}
        selectedLogoId={selectedLogoId}
        busy={busy}
        logoUploading={logoUploading}
        logoDeleting={logoDeleting}
        canAddLogo={canAddLogo}
        onUploadLogos={onUploadLogos}
        onPickLogo={onPickLogo}
        onRemoveLogo={onRemoveLogo}
      />

      <div className="product-block">
        <div className="product-block-head">
          <strong>素材图</strong>
        </div>
        {products.length > 0 ? (
          <div className="product-strip">
            {products.map((product, index) => (
              <div className="product-thumb" key={product.id}>
                <ZoomableImage
                  className="product-thumb-preview"
                  imageClassName="product-thumb-img"
                  src={product.url}
                  alt={product.original_name}
                  title={product.original_name}
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
            {products.length < 6 ? (
              <button type="button" className="product-add" onClick={onPickProducts}>
                <span className="upload-plus" aria-hidden="true" />
                <span>添加</span>
              </button>
            ) : null}
          </div>
        ) : (
          <button type="button" className="product-empty" onClick={onPickProducts}>
            <span className="upload-plus" aria-hidden="true" />
            <strong>从资产管理选择素材图</strong>
            <span>可多选</span>
          </button>
        )}
      </div>

      <PromptField
        label="设计需求"
        availability="必填"
        placeholder="例如：夏日冰美式新品海报，年轻、有设计感，标题写「夏日限定」"
        prompt={prompt}
        promptOptimized={promptOptimized}
        busy={busy}
        canOptimize={optimizationReady}
        optimizationBlockedReason={optimizationBlockedReason}
        optimizeContext={{
          brand_mode: brandMode,
          brand_id: brandMode === 'preset' ? selectedBrandId : null,
          logo_asset_id: selectedLogoId || null,
          style_reference_asset_ids:
            brandMode === 'reference' ? styleReferences.map((asset) => asset.id) : [],
          product_asset_ids: products.map((asset) => asset.id),
          platform_id: selectedPlatform?.id ?? selectedPlatformId ?? null,
          placement_id: selectedPlacementId || null,
        }}
        onPromptChange={onPromptChange}
      />

      <CandidateCountSelector
        value={candidateCount}
        busy={busy}
        onChange={onCandidateCountChange}
      />


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

    </section>
  )
}
