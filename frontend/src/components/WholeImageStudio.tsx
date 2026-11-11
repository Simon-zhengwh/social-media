import { useCallback, useEffect, useRef, useState } from 'react'
import AssetPicker from './AssetPicker'
import WholeChatPanel from './WholeChatPanel'
import WholeImageForm from './WholeImageForm'
import WholeModelInputPanel from './WholeModelInputPanel'
import WholeImageResultPanel from './WholeImageResultPanel'
import WholeVersionPanel from './WholeVersionPanel'
import OriginalReplacementPanel from './OriginalReplacementPanel'
import type { StoreSelection } from './StoreInfoSelector'
import {
  ApiError,
  createWholeImageEdit,
  createWholeImageTask,
  getWholeImageTask,
  listWholeImageTaskBatch,
  listWholeImageVersions,
  selectWholeImageOutput,
  selectWholeImageVersion,
  merchantIdFromLocation,
  uploadAsset,
} from '../api'
import { readPosterPreference, savePosterPreference } from '../preferences'
import type {
  Asset,
  BrandLogoImage,
  BrandMode,
  BrandStyle,
  ImageModel,
  MerchantProfile,
  PlatformPreset,
  Ratio,
  WholeImageTask,
  WholeImageVersion,
} from '../types'
import { DEFAULT_IMAGE_MODEL } from '../imageModels'

type Props = {
  merchantProfile: MerchantProfile | null
  brands: BrandStyle[]
  // Merchant-level logos: shared by every platform style, and the poster form's default.
  merchantLogos: BrandLogoImage[]
  brandsLoading: boolean
  brandsError: string | null
  platforms: PlatformPreset[]
  platformsLoading: boolean
  platformsError: string | null
  assets: Asset[]
  assetsLoading: boolean
  onLoadAssets: () => void
  onGoAssets: () => void
}

type StudioTab = 'chat' | 'original_replace' | 'versions' | 'model_input'

const MAX_PRODUCTS = 6

const TABS: Array<{ key: StudioTab; label: string }> = [
  { key: 'chat', label: '对话' },
  { key: 'original_replace', label: '原照替换' },
  { key: 'versions', label: '版本' },
  { key: 'model_input', label: '模型输入' },
]

function readWholeTaskId(): string | null {
  return new URLSearchParams(window.location.search).get('whole')
}

function updateWholeUrl(taskId: string | null) {
  const url = new URL(window.location.href)
  url.searchParams.set('view', 'whole')
  if (taskId) {
    url.searchParams.set('whole', taskId)
    url.searchParams.delete('task')
  } else {
    url.searchParams.delete('whole')
  }
  window.history.replaceState({}, '', url)
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message
  }
  if (error instanceof Error) {
    return error.message
  }
  return '请求失败，请稍后重试'
}

function assetFromTask(
  task: WholeImageTask,
  assetId?: string | null,
): Asset | null {
  if (!assetId) {
    return null
  }
  const image = task.model_input?.images.find((item) => item.asset_id === assetId)
  return {
    id: assetId,
    original_name: image?.name || '已保存的素材图',
    width: 0,
    height: 0,
    mime_type: 'image/png',
    url: image?.preview_url || task.preview_url || "",
    created_at: task.created_at,
  }
}

function logoFromAsset(asset: Asset): BrandLogoImage {
  return {
    asset_id: asset.id,
    original_name: asset.original_name,
    url: asset.url,
    width: asset.width,
    height: asset.height,
  }
}

export default function WholeImageStudio({
  merchantProfile,
  brands,
  merchantLogos,
  brandsLoading,
  brandsError,
  platforms,
  platformsLoading,
  platformsError,
  assets,
  assetsLoading,
  onLoadAssets,
  onGoAssets,
}: Props) {
  const [task, setTask] = useState<WholeImageTask | null>(null)
  const [versions, setVersions] = useState<WholeImageVersion[]>([])
  const [products, setProducts] = useState<Asset[]>([])
  // FB-006: the last poster selection is remembered per merchant on this browser.
  const merchantId = merchantIdFromLocation()
  const [preference] = useState(() => readPosterPreference(merchantId))
  const [selectedPlatformId, setSelectedPlatformId] = useState(() => preference?.platform_id ?? '')
  const [selectedPlacementId, setSelectedPlacementId] = useState(() => preference?.placement_id ?? '')
  const [selectedRatio, setSelectedRatio] = useState<Ratio>(() => preference?.ratio ?? '9:16')
  // Store facts stay off until the operator asks for them: printing an address is a per-poster
  // decision, so the default must not put one on a poster nobody vetted.
  const [storeSelection, setStoreSelection] = useState<StoreSelection>({
    enabled: false,
    placeId: '',
    address: true,
    phone: true,
  })
  const stores = merchantProfile?.brand_info?.stores ?? []
  const [prompt, setPrompt] = useState('')
  const [promptOptimized, setPromptOptimized] = useState(false)
  const [candidateCount, setCandidateCount] = useState(3)
  const [imageModel, setImageModel] = useState<ImageModel>(DEFAULT_IMAGE_MODEL)
  const [candidateTasks, setCandidateTasks] = useState<WholeImageTask[]>([])
  const [selectedBrandId, setSelectedBrandId] = useState(() => preference?.brand_id ?? '')
  const [selectedLogoId, setSelectedLogoId] = useState('')
  const [logoChoiceTouched, setLogoChoiceTouched] = useState(false)
  const [brandMode, setBrandMode] = useState<BrandMode>(() => preference?.brand_mode ?? 'preset')
  const [styleReferences, setStyleReferences] = useState<Asset[]>([])
  const [styleReferencesUploading, setStyleReferencesUploading] = useState(false)
  const [productsUploading, setProductsUploading] = useState(false)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [studioTab, setStudioTab] = useState<StudioTab>(() =>
    new URLSearchParams(window.location.search).get('tab') === 'original_replace' ? 'original_replace' : 'chat')
  const [creating, setCreating] = useState(() => Boolean(readWholeTaskId()))
  const [versionsLoading, setVersionsLoading] = useState(false)
  const [selectingOutput, setSelectingOutput] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const pollRef = useRef<number | null>(null)
  // Set while a saved task is being restored, so its configuration never rewrites the preference.
  const restoringRef = useRef(false)
  // Only an explicit operator change writes the preference; restored or auto-derived values never do.
  const preferenceTouchedRef = useRef(false)
  const touchPreference = () => {
    preferenceTouchedRef.current = true
  }
  const inFlight = useRef(false)
  const submitLock = useRef(false)
  const lastVersionRef = useRef<string | null>(null)

  // A remembered platform that no longer exists degrades to ``no platform`` while rendering, so no
  // effect has to reset state once the lists arrive.
  const platformResolved =
    !selectedPlatformId || platformsLoading || platformsError
      ? selectedPlatformId
      : platforms.some((item) => item.id === selectedPlatformId)
        ? selectedPlatformId
        : ''
  const selectedPlatform =
    platforms.find((item) => item.id === platformResolved) ?? null
  // A remembered placement outside the platform's own list falls back to the platform default.
  const placementResolved =
    selectedPlacementId &&
    (!selectedPlatform ||
      selectedPlatform.placements.some((item) => item.id === selectedPlacementId))
      ? selectedPlacementId
      : ''
  const effectivePlacementId =
    placementResolved || selectedPlatform?.default_placement_id || ''
  const brandsForPlatform = platformResolved
    ? brands.filter(
        (brand) =>
          brand.platform_id === platformResolved &&
          brand.placement_id === effectivePlacementId,
      )
    : []
  const effectiveBrandId =
    selectedBrandId && brandsForPlatform.some((brand) => brand.id === selectedBrandId)
      ? selectedBrandId
      : brandsForPlatform[0]?.id ?? ''
  // Brand mode does not change the logo source: only the merchant's configured logos are offered.
  const baseLogoOptions: BrandLogoImage[] = merchantLogos
  // The first configured logo is the default in every brand mode; an explicit choice sticks.
  const effectiveLogoId = logoChoiceTouched
    ? selectedLogoId
    : merchantLogos[0]?.asset_id ?? ''
  // A saved task may still reference an older logo that is no longer in the merchant set; keep it
  // selectable so opening that task does not silently drop its logo.
  const missingFromBase =
    Boolean(effectiveLogoId) &&
    !baseLogoOptions.some((logo) => logo.asset_id === effectiveLogoId)
  const selectedAssetLogo = missingFromBase
    ? assets.find((asset) => asset.id === effectiveLogoId) ?? null
    : null
  const taskRestoredAsset = missingFromBase && !selectedAssetLogo && task
    ? assetFromTask(task, effectiveLogoId)
    : null
  const restoredLogo: BrandLogoImage | null = selectedAssetLogo
    ? logoFromAsset(selectedAssetLogo)
    : taskRestoredAsset
      ? {
          asset_id: taskRestoredAsset.id,
          original_name: taskRestoredAsset.original_name,
          url: taskRestoredAsset.url,
          width: taskRestoredAsset.width,
          height: taskRestoredAsset.height,
        }
      : null
  // The submit and the form both read these, so they are derived once, after all state.
  const selectedStoreId = storeSelection.placeId || stores[0]?.place_id || ''
  const storeEnabled = storeSelection.enabled && Boolean(selectedStoreId)

  const logoOptions: BrandLogoImage[] = restoredLogo
    ? [...baseLogoOptions, restoredLogo]
    : baseLogoOptions
  const selectedPlacement =
    selectedPlatform?.placements.find((item) => item.id === effectivePlacementId) ??
    selectedPlatform?.placements.find((item) => item.id === selectedPlatform.default_placement_id) ??
    null

  /*
    Only explicit choices are persisted: browsing a saved task must not overwrite the pair the
    operator picked last. List loading also gates the write so an unresolved default cannot
    erase a valid cached platform, placement or style.
  */
  useEffect(() => {
    if (!preferenceTouchedRef.current) return
    if (restoringRef.current || platformsLoading || brandsLoading) return
    savePosterPreference(merchantId, {
      platform_id: platformResolved,
      placement_id: effectivePlacementId,
      ratio: selectedRatio,
      brand_mode: brandMode,
      brand_id: brandMode === 'preset' ? effectiveBrandId : '',
    })
  }, [
    brandMode,
    brandsLoading,
    effectiveBrandId,
    effectivePlacementId,
    merchantId,
    platformResolved,
    platformsLoading,
    selectedRatio,
  ])

  const loadVersions = useCallback(async (taskId: string) => {
    setVersionsLoading(true)
    try {
      setVersions(await listWholeImageVersions(taskId))
    } catch (reason: unknown) {
      setError(errorMessage(reason))
    } finally {
      setVersionsLoading(false)
    }
  }, [])

  useEffect(() => {
    const taskId = readWholeTaskId()
    if (!taskId) {
      return
    }
    let cancelled = false
    restoringRef.current = true
    getWholeImageTask(taskId)
      .then(async (restored) => {
        if (cancelled) {
          return
        }
        setTask(restored)
        setPrompt(restored.input.prompt ?? '')
        setPromptOptimized(restored.input.prompt_optimized === true)
        // A saved task is authoritative: "no platform" must clear a remembered platform too.
        setSelectedRatio(restored.input.ratio ?? '9:16')
        setCandidateCount(restored.input.candidate_count ?? 3)
        setImageModel(restored.input.image_model ?? DEFAULT_IMAGE_MODEL)
        setSelectedPlatformId(restored.platform?.id ?? '')
        setSelectedPlacementId(
          restored.platform
            ? restored.placement?.id ?? restored.platform.default_placement_id
            : '',
        )
        setSelectedBrandId(restored.input.brand_id ?? '')
        setSelectedLogoId(restored.input.logo_asset_id ?? '')
        setLogoChoiceTouched(true)
        const restoredBrandId = restored.input.brand_id ?? ''
        setBrandMode(
          restored.input.brand_mode ??
            (restoredBrandId
              ? 'preset'
              : (restored.input.style_reference_asset_ids?.length ?? 0) > 0
                ? 'reference'
                : 'none'),
        )
        setStyleReferences(
          (restored.input.style_reference_asset_ids ?? [])
            .map((id) => assetFromTask(restored, id))
            .filter((asset): asset is Asset => Boolean(asset)),
        )
        const restoredProducts = (restored.input.product_asset_ids ?? [])
          .map((id) => assetFromTask(restored, id))
          .filter((asset): asset is Asset => Boolean(asset))
        setProducts(restoredProducts)
        lastVersionRef.current = restored.current_version_id
        if (restored.current_version > 0) {
          await loadVersions(restored.id)
        }
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setError(errorMessage(reason))
        }
      })
      .finally(() => {
        restoringRef.current = false
        if (!cancelled) {
          setCreating(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [loadVersions])

  const candidateBatchId = task?.input.candidate_batch_id ?? null
  useEffect(() => {
    if (!candidateBatchId) {
      return
    }
    let cancelled = false
    const refresh = async () => {
      try {
        const list = await listWholeImageTaskBatch(candidateBatchId)
        if (!cancelled) setCandidateTasks(list)
      } catch {
        // Keep existing placeholders while the batch is still being created.
      }
    }
    void refresh()
    const interval = window.setInterval(refresh, 1800)
    return () => {
      cancelled = true
      window.clearInterval(interval)
    }
  }, [candidateBatchId])

  const visibleCandidates = candidateBatchId
    ? candidateTasks
    : task
      ? [task]
      : []
  const taskId = task?.id ?? null
  const taskStatus = task?.status ?? null
  const editRunning = task?.edit.status === 'running'

  useEffect(() => {
    const shouldPoll =
      taskId !== null &&
      (taskStatus === 'queued' || taskStatus === 'running' || editRunning)
    if (!shouldPoll) {
      if (pollRef.current !== null) {
        window.clearInterval(pollRef.current)
        pollRef.current = null
      }
      return
    }
    const tick = async () => {
      if (inFlight.current) {
        return
      }
      inFlight.current = true
      try {
        const next = await getWholeImageTask(taskId)
        setTask(next)
        if (next.current_version_id !== lastVersionRef.current) {
          lastVersionRef.current = next.current_version_id
          await loadVersions(next.id)
        }
      } catch (reason: unknown) {
        setError(errorMessage(reason))
      } finally {
        inFlight.current = false
      }
    }
    pollRef.current = window.setInterval(tick, 1500)
    return () => {
      if (pollRef.current !== null) {
        window.clearInterval(pollRef.current)
        pollRef.current = null
      }
    }
  }, [taskId, taskStatus, editRunning, loadVersions])

  const active = Boolean(task && (task.status === 'queued' || task.status === 'running'))
  const studioReady = Boolean(task?.status === 'succeeded' && task.current_version_id)
  const showStudioInspector = Boolean(studioReady || task?.model_input)
  const busy = creating || active || editRunning || productsUploading || selectingOutput
  const canEdit = Boolean(task?.status === 'succeeded' && task.current_version_id && task.current_output_id)

  const handleGenerate = async (
    promptOverride = prompt,
    optimizedOverride = promptOptimized,
  ) => {
    if (submitLock.current || busy) {
      return
    }
    if (!promptOverride.trim()) {
      setError('请先填写设计需求')
      return
    }
    if (products.length === 0) {
      setError('请先从资产管理选择素材图')
      return
    }
    if (brandMode === 'preset' && !brandsForPlatform.some((brand) => brand.id === effectiveBrandId)) {
      setError('请选择一个预设品牌风格')
      return
    }
    if (platformResolved && (!selectedPlatform || !selectedPlacement)) {
      setError('发布平台配置尚未就绪，请稍后重试或选择“由 AI 自由发挥”')
      return
    }
    submitLock.current = true
    setCreating(true)
    setError(null)
    setTask(null)
    setVersions([])
    lastVersionRef.current = null
    updateWholeUrl(null)
    try {
      const batchId = crypto.randomUUID()
      const created = await Promise.all(
        Array.from({ length: candidateCount }, (_, index) =>
          createWholeImageTask({
            image_model: imageModel,
            prompt: promptOverride,
            prompt_optimized: optimizedOverride,
            brand_id: brandMode === 'preset' ? effectiveBrandId : null,
            brand_mode: brandMode,
            logo_asset_id: effectiveLogoId || null,
            style_reference_asset_ids:
              brandMode === 'reference' ? styleReferences.map((asset) => asset.id) : [],
            product_asset_ids: products.map((asset) => asset.id),
            include_store_address: storeEnabled && storeSelection.address,
            include_store_phone: storeEnabled && storeSelection.phone,
            store_place_id: storeEnabled ? selectedStoreId : null,
            ratio: selectedPlatform ? undefined : selectedRatio,
            platform_id: selectedPlatform?.id ?? null,
            placement_id: selectedPlacement?.id ?? null,
            candidate_count: candidateCount,
            candidate_batch_id: batchId,
            candidate_index: index,
          }),
        ),
      )
      setCandidateTasks(created)
      setTask(created[0])
      updateWholeUrl(created[0].id)
    } catch (reason: unknown) {
      setError(errorMessage(reason))
    } finally {
      submitLock.current = false
      setCreating(false)
    }
  }

  const handleEdit = async (
    instruction: string,
    sourceVersionId = task?.current_version_id ?? null,
    referenceAssetIds: string[] = [],
  ): Promise<boolean> => {
    if (!task || !sourceVersionId || editRunning || submitLock.current) {
      return false
    }
    submitLock.current = true
    setError(null)
    try {
      const next = await createWholeImageEdit(
        task.id,
        instruction,
        sourceVersionId,
        sourceVersionId === task.current_version_id ? task.current_output_id : null,
        referenceAssetIds,
      )
      setTask(next)
      setStudioTab('chat')
      return true
    } catch (reason: unknown) {
      setError(errorMessage(reason))
      return false
    } finally {
      submitLock.current = false
    }
  }

  const handleRetry = async () => {
    if (!task) {
      return
    }
    if (task.edit.status === 'failed' && task.edit.instruction && task.edit.source_version_id) {
      await handleEdit(
        task.edit.instruction,
        task.edit.source_version_id,
        task.edit.reference_asset_ids ?? [],
      )
      return
    }
    if (task.status === 'failed') {
      setPrompt(task.input.prompt ?? prompt)
      setPromptOptimized(task.input.prompt_optimized === true)
      await handleGenerate(task.input.prompt ?? prompt, task.input.prompt_optimized === true)
    }
  }

  const handleSelectVersion = async (versionId: string) => {
    if (!task || busy || versionId === task.current_version_id) {
      return
    }
    setError(null)
    try {
      const next = await selectWholeImageVersion(task.id, versionId)
      setTask(next)
      lastVersionRef.current = next.current_version_id
    } catch (reason: unknown) {
      setError(errorMessage(reason))
    }
  }

  const handleSelectOutput = async (versionId: string, outputId: string) => {
    if (!task || selectingOutput) return
    setSelectingOutput(true)
    setError(null)
    try {
      const next = await selectWholeImageOutput(task.id, versionId, outputId)
      setTask(next)
      lastVersionRef.current = next.current_version_id
    } catch (reason: unknown) {
      setError(errorMessage(reason))
    } finally {
      setSelectingOutput(false)
    }
  }

  const handleSelectCandidate = (candidate: WholeImageTask) => {
    setTask(candidate)
    setVersions([])
    lastVersionRef.current = candidate.current_version_id
    updateWholeUrl(candidate.id)
  }

  const handleBrandChange = (brandId: string) => {
    touchPreference()
    const brand = brands.find((item) => item.id === brandId)
    setSelectedBrandId(brandId)
    setBrandMode('preset')
    if (brand?.platform_id) {
      const nextPlatform = platforms.find((item) => item.id === brand.platform_id)
      setSelectedPlatformId(brand.platform_id)
      setSelectedPlacementId(
        brand.placement_id || nextPlatform?.default_placement_id || '',
      )
    }
    setPromptOptimized(false)
  }

  const handleBrandModeChange = (mode: BrandMode) => {
    touchPreference()
    setBrandMode(mode)
    setPromptOptimized(false)
    if (mode === 'preset' && !selectedBrandId) {
      const next = brandsForPlatform.find((brand) => brand.is_default) ?? brandsForPlatform[0]
      setSelectedBrandId(next?.id ?? '')
    }
  }

  const handleUploadChatImages = async (files: File[]): Promise<Asset[]> => {
    const uploaded = await Promise.all(files.map((file) => uploadAsset(file)))
    onLoadAssets()
    return uploaded
  }

  const handleUploadStyleReferences = async (files: File[]) => {
    const remaining = Math.max(0, 3 - styleReferences.length)
    const accepted = files.slice(0, remaining)
    if (!accepted.length) return
    setStyleReferencesUploading(true)
    setError(null)
    try {
      const uploaded = await Promise.all(
        accepted.map((file) => uploadAsset(file)),
      )
      setStyleReferences((previous) => [...previous, ...uploaded].slice(0, 3))
      setPromptOptimized(false)
    } catch (reason: unknown) {
      setError(errorMessage(reason))
    } finally {
      setStyleReferencesUploading(false)
    }
  }

  const handleUploadProducts = async (files: File[]) => {
    const accepted = files.slice(0, Math.max(0, MAX_PRODUCTS - products.length))
    if (!accepted.length) return
    setProductsUploading(true)
    setError(null)
    try {
      // Uploads land in the asset library, so the picker and later posters can reuse them.
      const uploaded = await Promise.all(accepted.map((file) => uploadAsset(file)))
      setProducts((previous) => [...previous, ...uploaded].slice(0, MAX_PRODUCTS))
      setPromptOptimized(false)
      onLoadAssets()
    } catch (reason: unknown) {
      setError(errorMessage(reason))
    } finally {
      setProductsUploading(false)
    }
  }

  /*
    The logos are merchant-level: clearing one here only means "this poster does not use a logo".
    Selecting the active tile again clears it; deleting a logo belongs to the brand page.
  */
  const handleSelectLogo = (assetId: string) => {
    setSelectedLogoId(assetId)
    setLogoChoiceTouched(true)
    setPromptOptimized(false)
  }

  const openPicker = () => {
    setPickerOpen(true)
    if (assets.length === 0) {
      onLoadAssets()
    }
  }

  return (
    <main className={`workspace${showStudioInspector ? ' has-studio' : ''}`}>
      <WholeImageForm
        brands={brandsForPlatform}
        selectedBrandId={effectiveBrandId}
        brandsLoading={brandsLoading}
        brandsError={brandsError}
        brandMode={brandMode}
        styleReferences={styleReferences}
        styleReferencesUploading={styleReferencesUploading}
        logoOptions={logoOptions}
        selectedLogoId={effectiveLogoId}
        stores={stores}
        storeSelection={storeSelection}
        onStoreSelectionChange={(next) => setStoreSelection(next)}
        platforms={platforms}
        platformsLoading={platformsLoading}
        platformsError={platformsError}
        selectedPlatformId={platformResolved}
        selectedPlacementId={effectivePlacementId}
        selectedRatio={selectedRatio}
        products={products}
        productsUploading={productsUploading}
        prompt={prompt}
        promptOptimized={promptOptimized}
        candidateCount={candidateCount}
        imageModel={imageModel}
        busy={busy}
        onBrandChange={handleBrandChange}
        onBrandModeChange={handleBrandModeChange}
        onUploadStyleReferences={(files) => void handleUploadStyleReferences(files)}
        onRemoveStyleReference={(assetId) => {
          setStyleReferences((previous) => previous.filter((asset) => asset.id !== assetId))
          setPromptOptimized(false)
        }}
        onSelectLogo={handleSelectLogo}
        onUploadProducts={(files) => void handleUploadProducts(files)}
        onPlatformChange={(platformId) => {
          touchPreference()
          const next = platforms.find((item) => item.id === platformId)
          setSelectedPlatformId(platformId)
          setSelectedPlacementId(next?.default_placement_id ?? '')
          setSelectedBrandId('')
          setPromptOptimized(false)
        }}
        onPlacementChange={(value) => {
          touchPreference()
          setSelectedBrandId('')
          setSelectedPlacementId(value)
          setPromptOptimized(false)
        }}
        onRatioChange={(value) => {
          touchPreference()
          setSelectedRatio(value)
          setPromptOptimized(false)
        }}
        onPickProducts={openPicker}
        onRemoveProduct={(id) => {
          setProducts((previous) => previous.filter((item) => item.id !== id))
          setPromptOptimized(false)
        }}
        onPromptChange={(value, optimized) => {
          setPrompt(value)
          setPromptOptimized(optimized === true)
        }}
        onCandidateCountChange={setCandidateCount}
        onImageModelChange={setImageModel}
        onGenerate={() => void handleGenerate()}
      />

      <WholeImageResultPanel
        task={task}
        busy={creating}
        onRetry={() => void handleRetry()}
        candidates={visibleCandidates}
        onSelectCandidate={handleSelectCandidate}
        onSelectOutput={(versionId, outputId) => void handleSelectOutput(versionId, outputId)}
        selectingOutput={selectingOutput}
      />

      {showStudioInspector ? (
        <aside className="studio-inspector" aria-label="整图编辑工作台">
          <div className="studio-tabs" role="tablist">
            {TABS.map((tab) => (
              <button
                key={tab.key}
                type="button"
                role="tab"
                aria-selected={studioTab === tab.key}
                className={studioTab === tab.key ? 'is-active' : ''}
                onClick={() => {
                  setStudioTab(tab.key)
                  const url = new URL(window.location.href)
                  if (tab.key === 'original_replace') url.searchParams.set('tab', 'original_replace')
                  else url.searchParams.delete('tab')
                  window.history.replaceState({}, '', url)
                }}
              >
                {tab.label}
                {tab.key === 'versions' ? (
                  <span className="tab-count tnum">{versions.length || task?.current_version || 0}</span>
                ) : null}
              </button>
            ))}
          </div>
          <div className="studio-tab-body">
            {studioTab === 'chat' ? (
              <WholeChatPanel
                messages={task?.messages ?? []}
                edit={task?.edit ?? { status: 'idle' }}
                stage={task?.stage ?? 'editing'}
                disabled={!canEdit || busy}
                onUpload={handleUploadChatImages}
                onSend={(instruction, attachments) =>
                  handleEdit(
                    instruction,
                    task?.current_version_id ?? null,
                    attachments.map((asset) => asset.id),
                  )
                }
              />
            ) : null}
            {studioTab === 'original_replace' && task ? (
              <OriginalReplacementPanel key={task.id} task={task} assets={assets} assetsLoading={assetsLoading}
                onLoadAssets={onLoadAssets} onGoAssets={onGoAssets}
                onApplied={async (next) => {
                  setTask(next)
                  lastVersionRef.current = next.current_version_id
                  await loadVersions(next.id)
                }}
                onRefreshTask={async () => {
                  const next = await getWholeImageTask(task.id)
                  setTask(next)
                  lastVersionRef.current = next.current_version_id
                  await loadVersions(next.id)
                }} />
            ) : null}
            {studioTab === 'versions' ? (
              <WholeVersionPanel
                versions={versions}
                currentVersionId={task?.current_version_id ?? null}
                busy={versionsLoading || busy}
                onSelect={(versionId) => void handleSelectVersion(versionId)}
              />
            ) : null}
            {studioTab === 'model_input' ? (
              <WholeModelInputPanel
                modelInput={task?.model_input ?? null}
                outputs={task?.outputs ?? []}
                branches={task?.edit.branches}
              />
            ) : null}
          </div>
        </aside>
      ) : null}

      {error ? (
        <div className="global-error whole-image-error" role="alert">
          <span>{error}</span>
          <button type="button" onClick={() => setError(null)} aria-label="关闭提示">
            关闭
          </button>
        </div>
      ) : null}

      <AssetPicker
        key={pickerOpen ? 'whole-picker' : 'whole-closed'}
        open={pickerOpen}
        purpose="product"
        assets={assets}
        loading={assetsLoading}
        multi
        selectedIds={products.map((asset) => asset.id)}
        max={MAX_PRODUCTS}
        onClose={() => setPickerOpen(false)}
        onConfirm={(selected) => {
          setProducts(selected)
          setPromptOptimized(false)
          setPickerOpen(false)
        }}
        onGoAssets={() => {
          setPickerOpen(false)
          onGoAssets()
        }}
      />
    </main>
  )
}
