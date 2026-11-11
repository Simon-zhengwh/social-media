import PlatformSelector from './PlatformSelector'
import BrandModeSelector from './BrandModeSelector'
import PromptField from './PromptField'
import LogoSelector from './LogoSelector'
import StoreInfoSelector, { type StoreSelection } from './StoreInfoSelector'
import type {
  Asset,
  BrandMode,
  BrandStyle,
  BrandLogoImage,
  MerchantStore,
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
  busy: boolean
  onBrandChange: (brandId: string) => void
  onBrandModeChange: (mode: BrandMode) => void
  onUploadStyleReferences: (files: File[]) => void
  onRemoveStyleReference: (assetId: string) => void
  onSelectLogo: (assetId: string) => void
  onToggleLogo: (enabled: boolean) => void
  onPlatformChange: (platformId: string) => void
  onPlacementChange: (placementId: string) => void
  onRatioChange: (ratio: Ratio) => void
  onPickProducts: () => void
  onUploadProducts: (files: File[]) => void
  onRemoveProduct: (id: string) => void
  onPromptChange: (value: string, optimized?: boolean) => void
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
  busy,
  onBrandChange,
  onBrandModeChange,
  onUploadStyleReferences,
  onRemoveStyleReference,
  onSelectLogo,
  onToggleLogo,
  onPlatformChange,
  onPlacementChange,
  onRatioChange,
  onPickProducts,
  onUploadProducts,
  onRemoveProduct,
  onPromptChange,
}: Props) {
  const selectedPlatform = platforms.find((item) => item.id === selectedPlatformId)
  const brandReady =
    brandMode !== 'preset' ||
    (!brandsLoading &&
      !brandsError &&
      brands.some((brand) => brand.id === selectedBrandId))
  const optimizationReady =
    products.length > 0 && products.length <= 6 &&
    brandReady &&
    (!selectedPlatformId || Boolean(selectedPlatform))
  const optimizationBlockedReason =
    products.length === 0
      ? '请先添加参考图'
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
        allowReferenceMode={false}
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
        onToggleLogo={onToggleLogo}
      />

      <StoreInfoSelector
        stores={stores}
        value={storeSelection}
        disabled={busy}
        onChange={onStoreSelectionChange}
      />

      <PromptField
        label="设计需求"
        required
        placeholder="描述设计需求，输入 @ 选择图片并说明用途"
        prompt={prompt}
        promptOptimized={promptOptimized}
        busy={busy}
        images={products.map(asset => ({ assetId: asset.id, name: asset.original_name, url: asset.url }))}
        imagesUploading={productsUploading}
        onUploadImages={onUploadProducts}
        onPickImages={onPickProducts}
        onRemoveImage={onRemoveProduct}
        canOptimize={optimizationReady}
        optimizationBlockedReason={optimizationBlockedReason}
        optimizeContext={{
          brand_mode: brandMode,
          brand_id: brandMode === 'preset' ? selectedBrandId : null,
          logo_asset_id: selectedLogoId || null,
          reference_asset_ids: products.map(asset => asset.id),
          style_reference_asset_ids: [],
          product_asset_ids: [],
          platform_id: selectedPlatform?.id ?? selectedPlatformId ?? null,
          placement_id: selectedPlacementId || null,
          include_store_address: storeSelection.enabled && storeSelection.address,
          include_store_phone: storeSelection.enabled && storeSelection.phone,
          store_place_id: storeSelection.enabled ? (storeSelection.placeId || stores[0]?.place_id || null) : null,
        }}
        onPromptChange={onPromptChange}
      />


    </section>
  )
}
