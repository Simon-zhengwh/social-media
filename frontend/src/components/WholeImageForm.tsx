import type { ReactNode } from 'react'
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
  PosterSeriesMode,
  SeriesLogoPosition,
} from '../types'

type Props = {
  brands: BrandStyle[]
  selectedBrandId: string
  brandsLoading: boolean
  brandsError: string | null
  brandMode: BrandMode
  brandPresetUnavailable: boolean
  brandStudioHref: string
  styleReferences: Asset[]
  styleReferencesUploading: boolean
  logoOptions: BrandLogoImage[]
  selectedLogoId: string
  seriesLogoPosition: SeriesLogoPosition | null
  onSeriesLogoPositionChange: (position: SeriesLogoPosition | null) => void
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
  workspaceId?: string
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
  seriesEnabled?: boolean
  mode?: PosterSeriesMode
  modeLocked?: boolean
  onModeChange?: (mode: PosterSeriesMode) => void
  header?: ReactNode
  headerAction?: ReactNode
  footer?: ReactNode
}

export default function WholeImageForm({
  brands,
  selectedBrandId,
  brandsLoading,
  brandsError,
  brandMode,
  brandPresetUnavailable,
  brandStudioHref,
  styleReferences,
  styleReferencesUploading,
  logoOptions,
  selectedLogoId,
  seriesLogoPosition,
  onSeriesLogoPositionChange,
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
  workspaceId,
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
  seriesEnabled = false,
  mode = 'single',
  modeLocked = false,
  onModeChange,
  header,
  headerAction,
  footer,
}: Props) {
  const selectedPlatform = platforms.find((item) => item.id === selectedPlatformId)
  const selectedStoreId = (stores.find(store => store.place_id === storeSelection.placeId) ?? stores[0])?.place_id || ''
  const storeEnabled = storeSelection.enabled && Boolean(selectedStoreId)
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
      {header || seriesEnabled || headerAction ? (
        <div className="poster-sidebar-head">
          {header}
          {seriesEnabled || headerAction ? (
            <div className="poster-mode-row">
              {seriesEnabled ? (
                <div className="poster-mode-switch" role="group" aria-label="海报模式">
                  <button type="button" className={mode === 'single' ? 'is-active' : ''} aria-pressed={mode === 'single'}
                    disabled={busy || modeLocked} onClick={() => onModeChange?.('single')}>单张</button>
                  <button type="button" className={mode === 'series' ? 'is-active' : ''} aria-pressed={mode === 'series'}
                    disabled={busy || modeLocked} onClick={() => onModeChange?.('series')}>系列图</button>
                </div>
              ) : null}
              {headerAction}
            </div>
          ) : null}
        </div>
      ) : null}
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
        presetUnavailable={brandPresetUnavailable}
        brandStudioHref={brandStudioHref}
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
        position={seriesLogoPosition}
        onPositionChange={seriesEnabled && mode === 'series' ? onSeriesLogoPositionChange : undefined}
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
          workspace_id: workspaceId,
          brand_mode: brandMode,
          brand_id: brandMode === 'preset' ? selectedBrandId : null,
          logo_asset_id: selectedLogoId || null,
          reference_asset_ids: products.map(asset => asset.id),
          style_reference_asset_ids: [],
          product_asset_ids: [],
          platform_id: selectedPlatform?.id ?? selectedPlatformId ?? null,
          placement_id: selectedPlacementId || null,
          include_store_address: storeEnabled && storeSelection.address,
          include_store_phone: storeEnabled && storeSelection.phone,
          store_place_id: storeEnabled ? selectedStoreId : null,
        }}
        onPromptChange={onPromptChange}
      />

      {footer}
    </section>
  )
}
