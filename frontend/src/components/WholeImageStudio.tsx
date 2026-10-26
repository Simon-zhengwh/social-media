import { useCallback, useEffect, useRef, useState } from 'react'
import AssetPicker from './AssetPicker'
import WholeChatPanel from './WholeChatPanel'
import WholeImageForm from './WholeImageForm'
import WholeModelInputPanel from './WholeModelInputPanel'
import WholeImageResultPanel from './WholeImageResultPanel'
import WholeVersionPanel from './WholeVersionPanel'
import {
  ApiError,
  createWholeImageEdit,
  createWholeImageTask,
  deleteBrandLogo,
  getWholeImageTask,
  listWholeImageTaskBatch,
  listWholeImageVersions,
  selectWholeImageVersion,
  uploadAsset,
} from '../api'
import type {
  Asset,
  BrandLogoImage,
  BrandMode,
  BrandStyle,
  PlatformPreset,
  Ratio,
  WholeImageTask,
  WholeImageVersion,
} from '../types'
import { DEFAULT_IMAGE_MODEL } from '../imageModels'

type Props = {
  brands: BrandStyle[]
  brandsLoading: boolean
  brandsError: string | null
  platforms: PlatformPreset[]
  platformsLoading: boolean
  platformsError: string | null
  assets: Asset[]
  assetsLoading: boolean
  onLoadAssets: () => void
  onRefreshBrands: () => Promise<void>
  onGoAssets: () => void
}

type StudioTab = 'chat' | 'versions' | 'model_input'

const TABS: Array<{ key: StudioTab; label: string }> = [
  { key: 'chat', label: '对话' },
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
    url: `/api/assets/${assetId}/file`,
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
  brands,
  brandsLoading,
  brandsError,
  platforms,
  platformsLoading,
  platformsError,
  assets,
  assetsLoading,
  onLoadAssets,
  onRefreshBrands,
  onGoAssets,
}: Props) {
  const [task, setTask] = useState<WholeImageTask | null>(null)
  const [versions, setVersions] = useState<WholeImageVersion[]>([])
  const [products, setProducts] = useState<Asset[]>([])
  const [selectedPlatformId, setSelectedPlatformId] = useState('')
  const [selectedPlacementId, setSelectedPlacementId] = useState('')
  const [selectedRatio, setSelectedRatio] = useState<Ratio>('9:16')
  const [prompt, setPrompt] = useState('')
  const [promptOptimized, setPromptOptimized] = useState(false)
  const [candidateCount, setCandidateCount] = useState(3)
  const [candidateTasks, setCandidateTasks] = useState<WholeImageTask[]>([])
  const [selectedBrandId, setSelectedBrandId] = useState('')
  const [selectedLogoId, setSelectedLogoId] = useState('')
  const [logoChoiceTouched, setLogoChoiceTouched] = useState(false)
  const [brandMode, setBrandMode] = useState<BrandMode>('preset')
  const [styleReferences, setStyleReferences] = useState<Asset[]>([])
  const [styleReferencesUploading, setStyleReferencesUploading] = useState(false)
  const [logoUploading, setLogoUploading] = useState(false)
  const [logoDeleting, setLogoDeleting] = useState(false)
  const [pickerPurpose, setPickerPurpose] = useState<'product' | 'logo' | null>(null)
  const [studioTab, setStudioTab] = useState<StudioTab>('chat')
  const [creating, setCreating] = useState(() => Boolean(readWholeTaskId()))
  const [versionsLoading, setVersionsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const pollRef = useRef<number | null>(null)
  const inFlight = useRef(false)
  const submitLock = useRef(false)
  const lastVersionRef = useRef<string | null>(null)

  const selectedPlatform =
    platforms.find((item) => item.id === selectedPlatformId) ?? null
  const effectivePlacementId =
    selectedPlacementId || selectedPlatform?.default_placement_id || ''
  const brandsForPlatform = selectedPlatformId
    ? brands.filter(
        (brand) =>
          brand.platform_id === selectedPlatformId &&
          brand.placement_id === effectivePlacementId,
      )
    : []
  const effectiveBrandId =
    selectedBrandId && brandsForPlatform.some((brand) => brand.id === selectedBrandId)
      ? selectedBrandId
      : brandsForPlatform[0]?.id ?? ''
  const selectedBrand = brandsForPlatform.find((brand) => brand.id === effectiveBrandId) ?? null
  const availableLogos = [
    ...brands.flatMap((brand) => brand.logo_images),
    ...assets
      .map((asset) => ({
        asset_id: asset.id,
        original_name: asset.original_name,
        url: asset.url,
        width: asset.width,
        height: asset.height,
      })),
  ].filter(
    (logo, index, logos) =>
      logos.findIndex((candidate) => candidate.asset_id === logo.asset_id) === index,
  )
  const effectiveLogoId = logoChoiceTouched
    ? selectedLogoId
    : brandMode === 'preset'
      ? selectedBrand?.logo_images[0]?.asset_id ?? ''
      : ''
  const baseLogoOptions: BrandLogoImage[] =
    brandMode === 'preset'
      ? selectedBrand?.logo_images ?? []
      : availableLogos
  const missingFromBase =
    Boolean(effectiveLogoId) &&
    !baseLogoOptions.some((logo) => logo.asset_id === effectiveLogoId)
  const selectedAssetLogo = missingFromBase
    ? assets.find((asset) => asset.id === effectiveLogoId) ?? null
    : null
  const availableRestoredLogo = selectedAssetLogo
    ? logoFromAsset(selectedAssetLogo)
    : missingFromBase
      ? availableLogos.find((logo) => logo.asset_id === effectiveLogoId) ?? null
      : null
  const taskRestoredAsset =
    missingFromBase && !availableRestoredLogo && task
      ? assetFromTask(task, effectiveLogoId)
      : null
  const restoredLogo: BrandLogoImage | null =
    availableRestoredLogo ??
    (taskRestoredAsset
      ? {
          asset_id: taskRestoredAsset.id,
          original_name: taskRestoredAsset.original_name,
          url: taskRestoredAsset.url,
          width: taskRestoredAsset.width,
          height: taskRestoredAsset.height,
        }
      : null)
  const logoOptions: BrandLogoImage[] = restoredLogo
    ? [...baseLogoOptions, restoredLogo]
    : baseLogoOptions
  const selectedPlacement =
    selectedPlatform?.placements.find((item) => item.id === effectivePlacementId) ??
    selectedPlatform?.placements.find((item) => item.id === selectedPlatform.default_placement_id) ??
    null

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
    getWholeImageTask(taskId)
      .then(async (restored) => {
        if (cancelled) {
          return
        }
        setTask(restored)
        setPrompt(restored.input.prompt ?? '')
        setPromptOptimized(restored.input.prompt_optimized === true)
        setSelectedRatio(restored.input.ratio ?? '9:16')
        setCandidateCount(restored.input.candidate_count ?? 3)
        if (restored.platform?.id) {
          setSelectedPlatformId(restored.platform.id)
          setSelectedPlacementId(restored.placement?.id ?? restored.platform.default_placement_id)
        }
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
  const busy = creating || active || editRunning || logoUploading || logoDeleting
  const canEdit = Boolean(task?.status === 'succeeded' && task.current_version_id)

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
    if (selectedPlatformId && (!selectedPlatform || !selectedPlacement)) {
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
            image_model: DEFAULT_IMAGE_MODEL,
            prompt: promptOverride,
            prompt_optimized: optimizedOverride,
            brand_id: brandMode === 'preset' ? effectiveBrandId : null,
            brand_mode: brandMode,
            logo_asset_id: effectiveLogoId || null,
            style_reference_asset_ids:
              brandMode === 'reference' ? styleReferences.map((asset) => asset.id) : [],
            product_asset_ids: products.map((asset) => asset.id),
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
  ) => {
    if (!task || !sourceVersionId || editRunning || submitLock.current) {
      return
    }
    submitLock.current = true
    setError(null)
    try {
      const next = await createWholeImageEdit(task.id, instruction, sourceVersionId)
      setTask(next)
      setStudioTab('chat')
    } catch (reason: unknown) {
      setError(errorMessage(reason))
    } finally {
      submitLock.current = false
    }
  }

  const handleRetry = async () => {
    if (!task) {
      return
    }
    if (task.edit.status === 'failed' && task.edit.instruction && task.edit.source_version_id) {
      await handleEdit(task.edit.instruction, task.edit.source_version_id)
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

  const handleSelectCandidate = (candidate: WholeImageTask) => {
    setTask(candidate)
    setVersions([])
    lastVersionRef.current = candidate.current_version_id
    updateWholeUrl(candidate.id)
  }

  const handleBrandChange = (brandId: string) => {
    const brand = brands.find((item) => item.id === brandId)
    setSelectedBrandId(brandId)
    setBrandMode('preset')
    setSelectedLogoId('')
    setLogoChoiceTouched(false)
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
    setBrandMode(mode)
    setPromptOptimized(false)
    setSelectedLogoId('')
    setLogoChoiceTouched(mode !== 'preset')
    if (mode === 'preset' && !selectedBrandId) {
      const next = brandsForPlatform.find((brand) => brand.is_default) ?? brandsForPlatform[0]
      setSelectedBrandId(next?.id ?? '')
      setLogoChoiceTouched(false)
    }
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

  const handleUploadLogos = async (files: File[]) => {
    const file = files[0]
    if (!file) {
      return
    }

    setLogoUploading(true)
    setError(null)
    try {
      const uploaded = await uploadAsset(file)
      setSelectedLogoId(uploaded.id)
      setLogoChoiceTouched(true)
      setPromptOptimized(false)
      onLoadAssets()
    } catch (reason: unknown) {
      setError(errorMessage(reason))
    } finally {
      setLogoUploading(false)
    }
  }
  const handleRemoveLogo = async (assetId: string) => {
    const belongsToCurrentBrand =
      brandMode === 'preset' &&
      selectedBrand?.logo_images.some((logo) => logo.asset_id === assetId) === true

    setLogoDeleting(true)
    setError(null)
    try {
      if (belongsToCurrentBrand && selectedBrand) {
        await deleteBrandLogo(selectedBrand.id, assetId)
        await onRefreshBrands()
      }
      setSelectedLogoId('')
      setLogoChoiceTouched(brandMode !== 'preset')
      setPromptOptimized(false)
    } catch (reason: unknown) {
      setError(errorMessage(reason))
    } finally {
      setLogoDeleting(false)
    }
  }
  const openPicker = () => {
    setPickerPurpose('product')
    if (assets.length === 0) {
      onLoadAssets()
    }
  }
  const openLogoPicker = () => {
    setPickerPurpose('logo')
    onLoadAssets()
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
        logoUploading={logoUploading}
        logoDeleting={logoDeleting}
        canAddLogo
        platforms={platforms}
        platformsLoading={platformsLoading}
        platformsError={platformsError}
        selectedPlatformId={selectedPlatformId}
        selectedPlacementId={effectivePlacementId}
        selectedRatio={selectedRatio}
        products={products}
        prompt={prompt}
        promptOptimized={promptOptimized}
        candidateCount={candidateCount}
        busy={busy}
        onBrandChange={handleBrandChange}
        onBrandModeChange={handleBrandModeChange}
        onUploadStyleReferences={(files) => void handleUploadStyleReferences(files)}
        onRemoveStyleReference={(assetId) => {
          setStyleReferences((previous) => previous.filter((asset) => asset.id !== assetId))
          setPromptOptimized(false)
        }}
        onUploadLogos={(files) => void handleUploadLogos(files)}
        onPickLogo={openLogoPicker}
        onRemoveLogo={(assetId) => void handleRemoveLogo(assetId)}
        onPlatformChange={(platformId) => {
          const next = platforms.find((item) => item.id === platformId)
          setSelectedPlatformId(platformId)
          setSelectedPlacementId(next?.default_placement_id ?? '')
          setSelectedBrandId('')
          setSelectedLogoId('')
          setLogoChoiceTouched(false)
          setPromptOptimized(false)
        }}
        onPlacementChange={(value) => {
          setSelectedBrandId('')
          setSelectedLogoId('')
          setLogoChoiceTouched(false)
          setSelectedPlacementId(value)
          setPromptOptimized(false)
        }}
        onRatioChange={(value) => {
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
        onGenerate={() => void handleGenerate()}
      />

      <WholeImageResultPanel
        task={task}
        busy={creating}
        onRetry={() => void handleRetry()}
        candidates={visibleCandidates}
        onSelectCandidate={handleSelectCandidate}
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
                onClick={() => setStudioTab(tab.key)}
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
                onSend={(instruction) => void handleEdit(instruction)}
              />
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
              <WholeModelInputPanel modelInput={task?.model_input ?? null} />
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
        key={pickerPurpose ?? 'whole-closed'}
        open={pickerPurpose !== null}
        purpose={pickerPurpose ?? 'product'}
        assets={assets}
        loading={assetsLoading}
        multi={pickerPurpose !== 'logo'}
        selectedIds={pickerPurpose === 'logo' ? [] : products.map((asset) => asset.id)}
        max={pickerPurpose === 'logo' ? 1 : 6}
        onClose={() => setPickerPurpose(null)}
        onSelect={(asset) => {
          if (pickerPurpose !== 'logo') {
            return
          }
          setSelectedLogoId(asset.id)
          setLogoChoiceTouched(true)
          setPromptOptimized(false)
          setPickerPurpose(null)
        }}
        onConfirm={(selected) => {
          setProducts(selected)
          setPromptOptimized(false)
          setPickerPurpose(null)
        }}
        onGoAssets={() => {
          setPickerPurpose(null)
          onGoAssets()
        }}
      />
    </main>
  )
}
