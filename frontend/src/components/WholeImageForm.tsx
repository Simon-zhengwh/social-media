import type { ChangeEvent } from 'react'
import PlatformSelector from './PlatformSelector'
import BrandModeSelector from './BrandModeSelector'
import CandidateCountSelector from './CandidateCountSelector'
import ImageModelSelector from './ImageModelSelector'
import PromptField from './PromptField'
import LogoSelector from './LogoSelector'
import StoreInfoSelector, { type StoreSelection } from './StoreInfoSelector'
import { ZoomableImage } from './ImageLightbox'
import type {
  Asset,
  BrandMode,
  BrandStyle,
  BrandLogoImage,
  ImageModel,
  MerchantStore,
  PlatformPreset,
  Ratio,
} from '../types'

const MAX_PRODUCTS = 6

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
  stores: MerchantStore[]
  storeSelection: StoreSelection
  onStoreSelectionChange: (next: StoreSelection) => void
  platforms: PlatformPreset[]
  platformsLoading: boolean
  platformsError: string | null
  selectedPlatformId: string
  selectedPlacementId: string
  selectedRatio: Ratio
  products: Asset[]
  productsUploading: boolean
  prompt: string
  promptOptimized: boolean
  candidateCount: number
  imageModel: ImageModel
  busy: boolean
  onBrandChange: (brandId: string) => void
  onBrandModeChange: (mode: BrandMode) => void
  onUploadStyleReferences: (files: File[]) => void
  onRemoveStyleReference: (assetId: string) => void
  onSelectLogo: (assetId: string) => void
  onPlatformChange: (platformId: string) => void
  onPlacementChange: (placementId: string) => void
  onRatioChange: (ratio: Ratio) => void
  onPickProducts: () => void
  onUploadProducts: (files: File[]) => void
  onRemoveProduct: (id: string) => void
  onPromptChange: (value: string, optimized?: boolean) => void
  onCandidateCountChange: (value: number) => void
  onImageModelChange: (value: ImageModel) => void
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
  stores,
  storeSelection,
  onStoreSelectionChange,
  platforms,
  platformsLoading,
  platformsError,
  selectedPlatformId,
  selectedPlacementId,
  selectedRatio,
  products,
  productsUploading,
  prompt,
  promptOptimized,
  candidateCount,
  imageModel,
  busy,
  onBrandChange,
  onBrandModeChange,
  onUploadStyleReferences,
  onRemoveStyleReference,
  onSelectLogo,
  onPlatformChange,
  onPlacementChange,
  onRatioChange,
  onPickProducts,
  onUploadProducts,
  onRemoveProduct,
  onPromptChange,
  onCandidateCountChange,
  onImageModelChange,
  onGenerate,
}: Props) {
  const selectedPlatform = platforms.find((item) => item.id === selectedPlatformId)
  const brandReady =
    brandMode !== 'preset' ||
    (!brandsLoading &&
      !brandsError &&
      brands.some((brand) => brand.id === selectedBrandId))
  const handleUploadFiles = (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? [])
    if (files.length) onUploadProducts(files)
    event.target.value = ''
  }

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
        onSelectLogo={onSelectLogo}
      />

      <StoreInfoSelector
        stores={stores}
        value={storeSelection}
        disabled={busy}
        onChange={onStoreSelectionChange}
      />

      <div className="product-block">
        <div className="product-block-head">
          <strong>素材图</strong>
          <span>{products.length}/{MAX_PRODUCTS}</span>
        </div>
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
          {products.length < MAX_PRODUCTS ? (
            <>
              {/* Same entries whether or not images are already chosen, matching the reference UI. */}
              <label
                className={`product-add${busy || productsUploading ? ' is-disabled' : ''}`}
                title="上传素材图（部署环境会同步到素材库）"
              >
                <input
                  type="file"
                  multiple
                  accept="image/png,image/jpeg,image/webp"
                  disabled={busy || productsUploading}
                  onChange={handleUploadFiles}
                />
                <span className="upload-plus" aria-hidden="true" />
                <span>{productsUploading ? '上传中' : '上传'}</span>
              </label>
              <button type="button" className="product-add" onClick={onPickProducts} disabled={busy} title="从素材库选择素材图">
                <span className="product-add-glyph" aria-hidden="true">▦</span>
                <span>素材库</span>
              </button>
            </>
          ) : null}
        </div>
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
          include_store_address: storeSelection.enabled && storeSelection.address,
          include_store_phone: storeSelection.enabled && storeSelection.phone,
          store_place_id: storeSelection.enabled ? (storeSelection.placeId || stores[0]?.place_id || null) : null,
        }}
        onPromptChange={onPromptChange}
      />

      <CandidateCountSelector
        value={candidateCount}
        busy={busy}
        onChange={onCandidateCountChange}
      />

      <ImageModelSelector
        value={imageModel}
        busy={busy}
        onChange={onImageModelChange}
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
