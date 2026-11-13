import { useCallback, useEffect, useRef, useState } from 'react'
import AssetPicker from './AssetPicker'
import ConfirmDialog from './ConfirmDialog'
import WholeChatPanel from './WholeChatPanel'
import WholeImageForm from './WholeImageForm'
import ImageModelSelector from './ImageModelSelector'
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
  listWholeImageWorkspace,
  retryWholeImageTask,
  deleteWholeImageCandidate,
  listWholeImageVersions,
  selectWholeImageOutput,
  selectWholeImageVersion,
  merchantIdFromLocation,
  merchantPath,
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
import { DEFAULT_IMAGE_MODEL, IMAGE_MODEL_OPTIONS } from '../imageModels'
import { defaultModelCounts, restoreModelCounts, mergeCandidates, modelGenerationState,
  submitModelRound, type ModelSubmission } from '../modelGeneration'
import { candidateForModel } from '../modelGeneration'
import { hydrateImagePrompt, imagePromptIssue, serializeImagePrompt, type PromptImage } from '../imageMentions'
import { readPosterSession, savePosterSession, type PosterDraftForm, type PosterSession } from '../posterSession'

type Props = {
  userId: string
  active: boolean
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
  { key: 'chat', label: '改图' },
  { key: 'original_replace', label: '原照替换' },
  { key: 'versions', label: '版本' },
  { key: 'model_input', label: '模型输入' },
]

function readWholeTaskId(): string | null {
  return new URLSearchParams(window.location.search).get('whole')
}

function updateWholeUrl(taskId: string | null, workspaceId?: string, model?: ImageModel) {
  const url = new URL(window.location.href)
  url.searchParams.set('view', 'whole')
  if (workspaceId) url.searchParams.set('whole_workspace', workspaceId)
  if (model) url.searchParams.set('whole_model', model)
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
    original_name: image?.name || '已保存的参考图',
    width: 0,
    height: 0,
    mime_type: 'image/png',
    url: image?.preview_url || merchantPath(`/api/assets/${encodeURIComponent(assetId)}/file`),
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
  userId,
  active: visible,
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
  const merchantId = merchantIdFromLocation()
  const [session] = useState<PosterSession | null>(() => {
    const saved = readPosterSession(userId, merchantId)
    const params = new URLSearchParams(window.location.search)
    const taskId = params.get('whole')
    const workspaceId = params.get('whole_workspace')
    // Explicit task links take priority over a previously unfinished form.
    if (taskId || workspaceId) return {
      kind: 'task', taskId, workspaceId: workspaceId ?? '',
      selectedModel: (params.get('whole_model') ?? DEFAULT_IMAGE_MODEL) as ImageModel,
    }
    return saved
  })
  const draft = session?.kind === 'draft' ? session.form : null
  const mountedRef = useRef(false)
  const visibleRef = useRef(visible)
  useEffect(() => { visibleRef.current = visible }, [visible])
  const acceptedRef = useRef(session?.kind === 'task')
  const initialFormRef = useRef<string | null>(null)
  const draftStartedRef = useRef(Boolean(draft))
  const [sessionSaveFailed, setSessionSaveFailed] = useState(false)
  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])
  const [task, setTask] = useState<WholeImageTask | null>(null)
  const [versions, setVersions] = useState<WholeImageVersion[]>([])
  const [products, setProducts] = useState<Asset[]>(() => draft?.products ?? [])
  // FB-006: the last poster selection is remembered per merchant on this browser.
  const [preference] = useState(() => readPosterPreference(merchantId))
  const [selectedPlatformId, setSelectedPlatformId] = useState(() => draft?.selectedPlatformId ?? preference?.platform_id ?? '')
  const [selectedPlacementId, setSelectedPlacementId] = useState(() => draft?.selectedPlacementId ?? preference?.placement_id ?? '')
  const [selectedRatio, setSelectedRatio] = useState<Ratio>(() => draft?.selectedRatio ?? preference?.ratio ?? '9:16')
  // Store facts stay off until the operator asks for them: printing an address is a per-poster
  // decision, so the default must not put one on a poster nobody vetted.
  const [storeSelection, setStoreSelection] = useState<StoreSelection>(() => draft?.storeSelection ?? ({
    enabled: false,
    placeId: '',
    address: true,
    phone: true,
  }))
  const stores = merchantProfile?.brand_info?.stores ?? []
  const [prompt, setPrompt] = useState(() => draft?.prompt ?? '')
  const [promptOptimized, setPromptOptimized] = useState(() => draft?.promptOptimized ?? false)
  const [modelCounts, setModelCounts] = useState(() => draft?.modelCounts ?? defaultModelCounts())
  const [selectedModel, setSelectedModel] = useState<ImageModel>(() => {
    if (session?.kind === 'task') return session.selectedModel
    const model = draft?.selectedModel ?? DEFAULT_IMAGE_MODEL
    return IMAGE_MODEL_OPTIONS.find(option => option.value === model)?.value ?? DEFAULT_IMAGE_MODEL
  })
  const selectedModelRef = useRef(selectedModel)
  const modelSelections = useRef<Partial<Record<ImageModel, string>>>({})
  const [modelSubmissions, setModelSubmissions] = useState<Partial<Record<ImageModel, ModelSubmission>>>({})
  const workspaceRef = useRef<string>(session?.workspaceId || crypto.randomUUID())
  const modelLocks = useRef(new Set<ImageModel>())
  const deletedCandidates = useRef(new Set<string>())
  const [deletingCandidateId, setDeletingCandidateId] = useState<string | null>(null)
  // Deleting a candidate switches the current poster, so it asks first in an in-app dialog.
  const [pendingCandidateDelete, setPendingCandidateDelete] = useState<WholeImageTask | null>(null)
  const deleteLock = useRef(false)
  const [candidateTasks, setCandidateTasks] = useState<WholeImageTask[]>([])
  const [selectedBrandId, setSelectedBrandId] = useState(() => draft?.selectedBrandId ?? preference?.brand_id ?? '')
  const [selectedLogoId, setSelectedLogoId] = useState(() => draft?.selectedLogoId ?? '')
  const [logoChoiceTouched, setLogoChoiceTouched] = useState(() => draft?.logoChoiceTouched ?? false)
  const [brandMode, setBrandMode] = useState<BrandMode>(() => draft?.brandMode ?? (preference?.brand_mode === 'reference' ? 'none' : preference?.brand_mode ?? 'preset'))
  const [styleReferences, setStyleReferences] = useState<Asset[]>(() => draft?.styleReferences ?? [])
  const [styleReferencesUploading, setStyleReferencesUploading] = useState(false)
  const [productsUploading, setProductsUploading] = useState(false)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [studioTab, setStudioTab] = useState<StudioTab>(() =>
    new URLSearchParams(window.location.search).get('tab') === 'original_replace' ? 'original_replace' : 'chat')
  const [creating, setCreating] = useState(() => session?.kind === 'task')
  const [sessionRestored, setSessionRestored] = useState(() => session?.kind !== 'task')
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
  const versionRequestRef = useRef(0)

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

  useEffect(() => {
    if (acceptedRef.current || creating) return
    const form: PosterDraftForm = {
      products, prompt, promptOptimized, selectedPlatformId, selectedPlacementId, selectedRatio,
      selectedBrandId, brandMode, selectedLogoId, logoChoiceTouched, styleReferences,
      storeSelection, selectedModel, modelCounts,
    }
    const signature = JSON.stringify(form)
    if (initialFormRef.current === null) initialFormRef.current = signature
    if (!draftStartedRef.current && signature === initialFormRef.current) return
    draftStartedRef.current = true
    setSessionSaveFailed(!savePosterSession(userId, merchantId, { kind: 'draft', workspaceId: workspaceRef.current, form }))
  }, [userId, merchantId, creating, products, prompt, promptOptimized, selectedPlatformId, selectedPlacementId,
    selectedRatio, selectedBrandId, brandMode, selectedLogoId, logoChoiceTouched, styleReferences,
    storeSelection, selectedModel, modelCounts])

  useEffect(() => {
    if (!acceptedRef.current || creating || !sessionRestored) return
    setSessionSaveFailed(!savePosterSession(userId, merchantId, {
      kind: 'task', taskId: task?.id ?? null, workspaceId: workspaceRef.current, selectedModel,
    }))
    if (visible) updateWholeUrl(task?.id ?? null, workspaceRef.current, selectedModel)
  }, [visible, creating, sessionRestored, task?.id, selectedModel, userId, merchantId])

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
    const request = ++versionRequestRef.current
    setVersionsLoading(true)
    try {
      const next = await listWholeImageVersions(taskId)
      if (request === versionRequestRef.current) setVersions(next)
    } catch (reason: unknown) {
      if (request === versionRequestRef.current) setError(errorMessage(reason))
    } finally {
      if (request === versionRequestRef.current) setVersionsLoading(false)
    }
  }, [])

  useEffect(() => {
    const taskId = session?.kind === 'task' ? session.taskId : null
    const savedWorkspace = session?.kind === 'task' ? session.workspaceId : null
    if (!taskId && !savedWorkspace) return
    let cancelled = false
    restoringRef.current = true
    const restore = async () => {
        const savedTask = taskId ? await getWholeImageTask(taskId) : null
        const workspace = savedTask?.input.generation_workspace_id ?? savedTask?.input.candidate_batch_id ?? savedTask?.id ?? savedWorkspace!
        const restoredCandidates = await listWholeImageWorkspace(workspace)
        if (cancelled) return
        workspaceRef.current = workspace
        const model = savedTask ? (savedTask.input.image_model ?? DEFAULT_IMAGE_MODEL) : selectedModelRef.current
        selectedModelRef.current = model
        setSelectedModel(model)
        const selected = savedTask ?? candidateForModel(restoredCandidates, model)
        setTask(selected)
        if (selected) modelSelections.current[model] = selected.id
        setCandidateTasks(restoredCandidates)
        setSessionRestored(true)
        setModelCounts(restoredCandidates.length ? restoreModelCounts(restoredCandidates) : defaultModelCounts())
        const restored = savedTask ?? restoredCandidates[0]
        if (!restored) return
        setPromptOptimized(restored.input.prompt_optimized === true)
        setSelectedRatio(restored.input.ratio ?? '9:16')
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
          (restored.input.brand_mode === 'reference' ? 'none' : restored.input.brand_mode) ??
            (restoredBrandId ? 'preset' : 'none'),
        )
        setStyleReferences(
          (restored.input.style_reference_asset_ids ?? [])
            .map((id) => assetFromTask(restored, id))
            .filter((asset): asset is Asset => Boolean(asset)),
        )
        const referenceIds = restored.input.reference_asset_ids ?? [...new Set([
          ...(restored.input.product_asset_ids ?? []), ...(restored.input.style_reference_asset_ids ?? []),
        ])]
        const restoredProducts = referenceIds
          .map((id) => assetFromTask(restored, id))
          .filter((asset): asset is Asset => Boolean(asset))
        setProducts(restoredProducts)
        setPrompt(hydrateImagePrompt(restored.input.prompt ?? '', restoredProducts.map(asset => ({ assetId: asset.id, name: asset.original_name, url: asset.url }))))
        lastVersionRef.current = selected?.current_version_id ?? null
        if (selected && selected.current_version > 0) await loadVersions(selected.id)
    }
    void restore().catch((reason: unknown) => {
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
  }, [loadVersions, session])

  const candidateBatchId = task?.input.candidate_batch_id ?? null
  const workspaceId = task?.input.generation_workspace_id ?? candidateTasks.find(item => item.input.generation_workspace_id)?.input.generation_workspace_id ?? candidateTasks[0]?.input.candidate_batch_id ?? candidateTasks[0]?.id ?? null
  const workspaceRunning = candidateTasks.some(candidate => candidate.status === 'queued' || candidate.status === 'running') ||
    Object.values(modelSubmissions).some(submission => submission && !submission.error)
  useEffect(() => {
    if ((!workspaceId && !candidateBatchId) || (!visible && !workspaceRunning)) {
      return
    }
    let cancelled = false
    const refresh = async () => {
      try {
        const list = workspaceId
          ? await listWholeImageWorkspace(workspaceId)
          : await listWholeImageTaskBatch(candidateBatchId!)
        if (!cancelled) setCandidateTasks(previous => mergeCandidates(previous, list).filter(item => !deletedCandidates.current.has(item.id)))
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
  }, [workspaceId, candidateBatchId, visible, workspaceRunning])

  const visibleCandidates = mergeCandidates(candidateTasks, task ? [task] : [])
  const modelCandidates = visibleCandidates.filter(candidate => (candidate.input.image_model ?? DEFAULT_IMAGE_MODEL) === selectedModel)
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
        if (cancelled || deletedCandidates.current.has(next.id)) return
        setTask(next)
        setCandidateTasks(previous => mergeCandidates(previous, [next]))
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
    let cancelled = false
    pollRef.current = window.setInterval(tick, 1500)
    return () => {
      cancelled = true
      if (pollRef.current !== null) {
        window.clearInterval(pollRef.current)
        pollRef.current = null
      }
    }
  }, [taskId, taskStatus, editRunning, loadVersions])

  const active = Boolean(task && (task.status === 'queued' || task.status === 'running'))
  const busy = creating || active || editRunning || productsUploading || selectingOutput
  const formBusy = creating || productsUploading || styleReferencesUploading
  const promptImages: PromptImage[] = products.map(asset => ({ assetId: asset.id, name: asset.original_name, url: asset.url }))
  const generationDisabled = formBusy || !prompt.trim() || products.length === 0 || products.length > MAX_PRODUCTS || Boolean(imagePromptIssue(prompt, promptImages)) ||
    (brandMode === 'preset' && (brandsLoading || Boolean(brandsError) || !brandsForPlatform.some(brand => brand.id === effectiveBrandId))) ||
    (Boolean(platformResolved) && (!selectedPlatform || !selectedPlacement))
  const canEdit = Boolean(task?.status === 'succeeded' && task.current_version_id && task.current_output_id)
  const currentOutput = task?.outputs.find(output => output.id === task.current_output_id) ?? task?.outputs[0]
  const currentPreview = currentOutput?.preview_url ?? task?.preview_url
  const currentPromptImage: PromptImage | null = task && currentPreview ? {
    assetId: `current:${task.id}:${task.current_version_id}:${task.current_output_id}`,
    name: '当前海报', url: currentPreview, removable: false,
  } : null

  const rememberAcceptedTask = (candidate: WholeImageTask | null) => {
    if (!mountedRef.current) return
    acceptedRef.current = true
    const model = selectedModelRef.current
    const selectedId = modelSelections.current[model] ??
      (candidate && (candidate.input.image_model ?? DEFAULT_IMAGE_MODEL) === model ? candidate.id : null)
    setSessionSaveFailed(!savePosterSession(userId, merchantId, {
      kind: 'task', taskId: selectedId, workspaceId: workspaceRef.current, selectedModel: model,
    }))
  }

  const handleGenerate = async (onlyModel?: ImageModel) => {
    if (formBusy) {
      return
    }
    if (!prompt.trim()) {
      setError('请先填写设计需求')
      return
    }
    if (products.length === 0) {
      setError('请先添加参考图')
      return
    }
    if (products.length > MAX_PRODUCTS) {
      setError(`最多使用 ${MAX_PRODUCTS} 张参考图，请移除多余图片`)
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
    let submittedPrompt: string
    try { submittedPrompt = serializeImagePrompt(prompt, promptImages) }
    catch (reason: unknown) { setError(errorMessage(reason)); return }
    const models = IMAGE_MODEL_OPTIONS.filter(option =>
      (!onlyModel || option.value === onlyModel) && (modelCounts[option.value] ?? 0) > 0 &&
      !modelLocks.current.has(option.value) && !modelGenerationState(visibleCandidates, option.value).running)
    const payload = {
            prompt: submittedPrompt,
            prompt_optimized: promptOptimized,
            brand_id: brandMode === 'preset' ? effectiveBrandId : null,
            brand_mode: brandMode,
            logo_asset_id: effectiveLogoId || null,
            reference_asset_ids: products.map(asset => asset.id),
            style_reference_asset_ids: [],
            product_asset_ids: [],
            include_store_address: storeEnabled && storeSelection.address,
            include_store_phone: storeEnabled && storeSelection.phone,
            store_place_id: storeEnabled ? selectedStoreId : null,
            ratio: selectedPlatform ? undefined : selectedRatio,
            platform_id: selectedPlatform?.id ?? null,
            placement_id: selectedPlacement?.id ?? null,
    }
    await Promise.all(models.map(async ({ value: model }) => {
      const count = modelCounts[model] ?? 0
      const batchId = crypto.randomUUID()
      modelLocks.current.add(model)
      setModelSubmissions(previous => ({ ...previous, [model]: { batchId, count } }))
      let submissionError: string | undefined
      try {
        const result = await submitModelRound(payload, model, count, workspaceRef.current, batchId, async (input) => {
          const created = await createWholeImageTask(input)
          // Each accepted request ends the draft immediately, even if a sibling request fails.
          rememberAcceptedTask(created)
          return created
        })
        if (!mountedRef.current) return
        let created = result.tasks
        if (result.errors.length) {
          // Reconcile a response lost after insertion before offering a manual new attempt.
          try { created = mergeCandidates(created, await listWholeImageWorkspace(workspaceRef.current)) } catch { /* no confirmed tasks yet */ }
          if (!mountedRef.current) return
          const confirmed = created.filter(item => item.input.candidate_batch_id === batchId).length
          if (confirmed < count) submissionError = `${count - confirmed} 张提交未确认，请先核对任务中心再手动重试。${errorMessage(result.errors[0])}`
        }
        created = created.filter(item => !deletedCandidates.current.has(item.id))
        setCandidateTasks(previous => mergeCandidates(previous, created).filter(item => !deletedCandidates.current.has(item.id)))
        const nextForModel = candidateForModel(created, selectedModelRef.current, modelSelections.current[selectedModelRef.current])
        setTask(previous => previous ?? nextForModel)
        // Only the first submission establishes the URL; subsequent rounds preserve the main image.
        if (created.length) rememberAcceptedTask(nextForModel)
        if (visibleRef.current && !readWholeTaskId() && nextForModel) {
          modelSelections.current[selectedModelRef.current] = nextForModel.id
          updateWholeUrl(nextForModel.id, workspaceRef.current, selectedModelRef.current)
        }
      } catch (reason: unknown) {
        submissionError = errorMessage(reason)
      } finally {
        modelLocks.current.delete(model)
        setModelSubmissions(previous => {
          const next = { ...previous }
          if (submissionError) next[model] = { batchId, count, error: submissionError }
          else delete next[model]
          return next
        })
      }
    }))
  }

  const handleEdit = async (
    instruction: string,
    sourceVersionId = task?.current_version_id ?? null,
    referenceAssetIds: string[] = [],
  ): Promise<boolean> => {
    if (!task || !sourceVersionId || editRunning || submitLock.current || deleteLock.current) {
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
        'unified',
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

  const handleRetry = async (candidate: WholeImageTask) => {
    if (submitLock.current || deleteLock.current) return
    submitLock.current = true
    try {
      const next = await retryWholeImageTask(candidate.id)
      setTask(previous => previous?.id === next.id ? next : previous)
      setCandidateTasks(previous => mergeCandidates(previous, [next]))
    } catch (reason: unknown) { setError(errorMessage(reason)) }
    finally { submitLock.current = false }
  }

  const handleSelectVersion = async (versionId: string) => {
    if (!task || busy || versionId === task.current_version_id) {
      return
    }
    setError(null)
    try {
      const next = await selectWholeImageVersion(task.id, versionId)
      setTask(previous => previous?.id === next.id ? next : previous)
      setCandidateTasks(previous => mergeCandidates(previous, [next]))
      if (readWholeTaskId() === next.id) lastVersionRef.current = next.current_version_id
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
    if (editRunning || submitLock.current || selectingOutput || deleteLock.current) return
    const model = (candidate.input.image_model ?? DEFAULT_IMAGE_MODEL)
    selectedModelRef.current = model
    setSelectedModel(model)
    modelSelections.current[model] = candidate.id
    setTask(candidate)
    setVersions([])
    ++versionRequestRef.current
    setVersionsLoading(false)
    lastVersionRef.current = candidate.current_version_id
    updateWholeUrl(candidate.id, workspaceRef.current, model)
    if (candidate.current_version > 0) void loadVersions(candidate.id)
  }

  const handleSelectModel = (model: ImageModel) => {
    if (creating || editRunning || submitLock.current || selectingOutput || deleteLock.current || model === selectedModelRef.current) return
    selectedModelRef.current = model
    setSelectedModel(model)
    const next = candidateForModel(visibleCandidates, model, modelSelections.current[model])
    setTask(next)
    setVersions([])
    ++versionRequestRef.current
    setVersionsLoading(false)
    lastVersionRef.current = next?.current_version_id ?? null
    if (next) modelSelections.current[model] = next.id
    if (acceptedRef.current) updateWholeUrl(next?.id ?? null, workspaceRef.current, model)
    if (next?.current_version) void loadVersions(next.id)
  }

  const handleDeleteCandidate = (candidate: WholeImageTask) => {
    if (deleteLock.current || submitLock.current || candidate.status === 'queued' || candidate.status === 'running' || candidate.edit.status === 'running') return
    setPendingCandidateDelete(candidate)
  }

  const confirmDeleteCandidate = async () => {
    const candidate = pendingCandidateDelete
    if (!candidate) return
    deleteLock.current = true
    setDeletingCandidateId(candidate.id)
    try {
      await deleteWholeImageCandidate(candidate.id)
      deletedCandidates.current.add(candidate.id)
      const remaining = visibleCandidates.filter(item => item.id !== candidate.id)
      setCandidateTasks(previous => previous.filter(item => item.id !== candidate.id))
      if (task?.id === candidate.id) {
        const next = candidateForModel(remaining, selectedModelRef.current)
        setTask(next)
        setVersions([])
        ++versionRequestRef.current
        setVersionsLoading(false)
        lastVersionRef.current = next?.current_version_id ?? null
        if (next) modelSelections.current[selectedModelRef.current] = next.id
        updateWholeUrl(next?.id ?? null, workspaceRef.current, selectedModelRef.current)
        if (next?.current_version) void loadVersions(next.id)
      }
    } catch (reason: unknown) { setError(errorMessage(reason)) }
    finally { deleteLock.current = false; setDeletingCandidateId(null); setPendingCandidateDelete(null) }
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
      setProducts((previous) => [...new Map([...previous, ...uploaded].map(asset => [asset.id, asset])).values()].slice(0, MAX_PRODUCTS))
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

  /**
   * The logo switch: off means this poster carries no logo, on restores the previous pick (or the
   * merchant's first logo). "No logo" is an explicit choice, hence logoChoiceTouched.
   */
  const handleToggleLogo = (enabled: boolean) => {
    setLogoChoiceTouched(true)
    const remembered = logoOptions.some((logo) => logo.asset_id === selectedLogoId)
      ? selectedLogoId
      : ''
    setSelectedLogoId(enabled ? remembered || logoOptions[0]?.asset_id || '' : '')
    setPromptOptimized(false)
  }

  const openPicker = () => {
    setPickerOpen(true)
    if (assets.length === 0) {
      onLoadAssets()
    }
  }

  return (
    <main className="workspace poster-workspace has-studio">
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
        busy={formBusy}
        onBrandChange={handleBrandChange}
        onBrandModeChange={handleBrandModeChange}
        onUploadStyleReferences={(files) => void handleUploadStyleReferences(files)}
        onRemoveStyleReference={(assetId) => {
          setStyleReferences((previous) => previous.filter((asset) => asset.id !== assetId))
          setPromptOptimized(false)
        }}
        onSelectLogo={handleSelectLogo}
        onToggleLogo={handleToggleLogo}
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
      />

      <div className="poster-editor">
        <ImageModelSelector
          counts={modelCounts}
          tasks={visibleCandidates}
          submissions={modelSubmissions}
          disabled={generationDisabled}
          onChange={(model, count) => setModelCounts(previous => ({ ...previous, [model]: count }))}
          onGenerate={(model) => void handleGenerate(model)}
          selectedModel={selectedModel}
          selectionDisabled={creating || editRunning || selectingOutput || deletingCandidateId !== null}
          onSelectModel={handleSelectModel}
        />
      <WholeImageResultPanel
        task={task}
        selectedModel={selectedModel}
        busy={creating}
        onRetry={(candidate) => void handleRetry(candidate)}
        candidates={modelCandidates}
        onSelectCandidate={handleSelectCandidate}
        onSelectOutput={(versionId, outputId) => void handleSelectOutput(versionId, outputId)}
        selectingOutput={selectingOutput}
        selectingCandidateDisabled={!visible || editRunning || selectingOutput}
        deletingCandidateId={deletingCandidateId}
        onDeleteCandidate={(candidate) => void handleDeleteCandidate(candidate)}
      />

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
              <>
              <WholeChatPanel
                key={task?.id}
                messages={task?.messages ?? []}
                edit={task?.edit ?? { status: 'idle' }}
                stage={task?.stage ?? 'editing'}
                disabled={!canEdit || busy}
                onUpload={handleUploadChatImages}
                currentImage={currentPromptImage}
                assets={assets}
                assetsLoading={assetsLoading}
                onLoadAssets={onLoadAssets}
                onGoAssets={onGoAssets}
                onSend={(instruction, attachments) =>
                  handleEdit(
                    instruction,
                    task?.current_version_id ?? null,
                    attachments.map((asset) => asset.id),
                  )
                }
              />
              </>
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
      </div>

      {pendingCandidateDelete ? (
        <ConfirmDialog
          title={pendingCandidateDelete.id === task?.id ? '删除当前主图候选？' : '删除这张候选图？'}
          description={
            pendingCandidateDelete.id === task?.id
              ? '删除后会切换到剩余候选，其他模型和版本不受影响。'
              : '删除后这张候选不再出现在工作台和任务中心，其他图片不受影响。'
          }
          confirmLabel="删除候选"
          busyLabel="删除中…"
          busy={deletingCandidateId !== null}
          onConfirm={() => void confirmDeleteCandidate()}
          onCancel={() => { if (deletingCandidateId === null) setPendingCandidateDelete(null) }}
        />
      ) : null}

      {error ? (
        <div className="global-error whole-image-error" role="alert">
          <span>{error}</span>
          <button type="button" onClick={() => setError(null)} aria-label="关闭提示">
            关闭
          </button>
        </div>
      ) : null}

      {sessionSaveFailed ? (
        <div className="global-error whole-image-error" role="alert">
          当前浏览器无法暂存编辑内容，刷新页面后可能丢失。
        </div>
      ) : null}

      <AssetPicker
        key={pickerOpen ? 'whole-picker' : 'whole-closed'}
        open={pickerOpen}
        purpose="product"
        title="选择参考图"
        assets={[...new Map([...products, ...assets].map(asset => [asset.id, asset])).values()]}
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
