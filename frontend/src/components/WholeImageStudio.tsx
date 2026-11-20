import { useCallback, useEffect, useRef, useState } from 'react'
import AssetPicker from './AssetPicker'
import ConfirmDialog from './ConfirmDialog'
import WholeChatPanel from './WholeChatPanel'
import WholeChatHistory from './WholeChatHistory'
import WholeImageForm from './WholeImageForm'
import PosterCostPanel from './PosterCostPanel'
import ImageModelSelector from './ImageModelSelector'
import WholeModelInputPanel from './WholeModelInputPanel'
import WholeImageResultPanel from './WholeImageResultPanel'
import PosterFinalizationControl from './PosterFinalizationControl'
import OriginalReplacementPanel from './OriginalReplacementPanel'
import PosterImageNavigator, { type PosterImageBatch } from './PosterImageNavigator'
import type { StoreSelection } from './StoreInfoSelector'
import {
  ApiError,
  createPosterSeriesRun,
  createWholeImageEdit,
  createWholeImageTask,
  getPosterSeriesConfig,
  getPosterSeriesWorkspace,
  getPosterWorkspaceMode,
  getWholeImageTask,
  getWholeImageWorkspaceTitle,
  listWholeImageTaskBatch,
  listWholeImageWorkspace,
  renameWholeImageWorkspace,
  retryPosterSeriesPage,
  retryWholeImageTask,
  deleteWholeImageCandidate,
  listWholeImageVersions,
  savePosterSeriesHtmlPage,
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
  PosterSeriesGroup,
  PosterSeriesMode,
  PosterSeriesPage,
  PosterSeriesWorkspace,
  Ratio,
  WholeImageTask,
  WholeImageVersion,
} from '../types'
import { DEFAULT_IMAGE_MODEL, IMAGE_MODEL_OPTIONS, normalizeImageModel } from '../imageModels'
import { candidateForModel, candidateRounds, defaultModelCounts, imageModelLabel, restoreModelCounts,
  mergeCandidates, modelGenerationState, submitModelRound, type ModelSubmission } from '../modelGeneration'
import { hydrateImagePrompt, serializeImagePrompt, type PromptImage } from '../imageMentions'
import { generationBlockers, presetStyleUnavailable } from '../generationBlockers'
import { formatPosterTitle, readPosterSession, savePosterSession, type PosterDraftForm, type PosterSession } from '../posterSession'
import {
  buildSeriesRunPayload,
  defaultGroupPage,
  htmlSaveBody,
  mergeSeriesPage,
  modeAfterServer,
  replacementDefaults,
  replacementPool,
  resolveSeriesSelection,
  selectionFromUrl,
  seriesContentPool,
  seriesNeedsPoll,
  seriesPageLabel,
  seriesStatusLabel,
  taskAssetPool,
} from '../posterSeries'

type Props = {
  userId: string
  active: boolean
  newDesignHref: string
  brandStudioHref: string
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

type StudioTab = 'chat_history' | 'original_replace' | 'model_input' | 'costs'

const TABS: Array<{ key: StudioTab; label: string }> = [
  { key: 'chat_history', label: '修改记录' },
  { key: 'original_replace', label: '原照替换' },
  { key: 'model_input', label: '模型输入' },
  { key: 'costs', label: '费用' },
]

function readWholeTaskId(): string | null {
  return new URLSearchParams(window.location.search).get('whole')
}

function updateWholeUrl(
  taskId: string | null,
  workspaceId?: string,
  model?: ImageModel,
  series?: { groupId: string | null; pageId: string | null } | null,
) {
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
  // Series deep links carry the group and page so a reload lands on the same page.
  if (series?.groupId) url.searchParams.set('series_group', series.groupId)
  else url.searchParams.delete('series_group')
  if (series?.pageId) url.searchParams.set('series_page', series.pageId)
  else url.searchParams.delete('series_page')
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

function isWorkspaceModeConflict(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409 && (
    error.code === 'workspace_mode_locked' ||
    error.message === '当前任务已锁定为另一种海报模式，请新建海报' ||
    error.message === '当前任务已按单张海报创建，请新建海报使用系列图'
  )
}

function assetFromTask(
  task: WholeImageTask | null,
  assetId?: string | null,
): Asset | null {
  if (!assetId) {
    return null
  }
  const image = task?.model_input?.images.find((item) => item.asset_id === assetId)
  return {
    id: assetId,
    original_name: image?.name || '已保存的参考图',
    width: 0,
    height: 0,
    mime_type: 'image/png',
    url: image?.preview_url || merchantPath(`/api/assets/${encodeURIComponent(assetId)}/file`),
    created_at: task?.created_at ?? '',
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
  newDesignHref,
  brandStudioHref,
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
      selectedModel: normalizeImageModel(params.get('whole_model')) as ImageModel,
      mode: params.has('series_group') ? 'series' : undefined,
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
  const [title, setTitle] = useState(() => session?.kind === 'task' ? '' : session?.title ?? formatPosterTitle(new Date()))
  const [titleEdit, setTitleEdit] = useState<string | null>(null)
  const [savingTitle, setSavingTitle] = useState(false)
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
    if (session?.kind === 'task') return normalizeImageModel(session.selectedModel)
    const model = draft?.selectedModel ?? DEFAULT_IMAGE_MODEL
    return IMAGE_MODEL_OPTIONS.find(option => option.value === model)?.value ?? DEFAULT_IMAGE_MODEL
  })
  const selectedModelRef = useRef(selectedModel)
  const modelSelections = useRef<Partial<Record<ImageModel, string>>>({})
  const [modelSubmissions, setModelSubmissions] = useState<Partial<Record<ImageModel, ModelSubmission>>>({})
  const [initialWorkspaceId] = useState(() => session?.workspaceId || crypto.randomUUID())
  const workspaceRef = useRef(initialWorkspaceId)
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
  const [studioTab, setStudioTab] = useState<StudioTab | 'auto' | null>(() =>
    new URLSearchParams(window.location.search).get('tab') === 'original_replace' ? 'original_replace' : 'auto')
  const [creating, setCreating] = useState(() => session?.kind === 'task')
  const [sessionRestored, setSessionRestored] = useState(() => session?.kind !== 'task')
  const [versionsLoading, setVersionsLoading] = useState(false)
  const [selectingOutput, setSelectingOutput] = useState(false)
  const [error, setError] = useState<string | null>(null)

  /*
    Poster series is opt-in per server configuration. The workspace mode is read from the
    server so a refresh (or a task-center deep link) opens the mode the task was created
    with; the first accepted submission locks it.
  */
  const initialSeriesSelection = selectionFromUrl(window.location.search)
  const [seriesEnabled, setSeriesEnabled] = useState(false)
  const [mode, setMode] = useState<PosterSeriesMode>(() =>
    draft?.mode ?? (session?.kind === 'task' ? session.mode ?? 'single' : 'single'))
  const [modeLocked, setModeLocked] = useState(() => session?.kind === 'task')
  const [series, setSeries] = useState<PosterSeriesWorkspace | null>(null)
  const [selectedGroupId, setSelectedGroupId] = useState<string | null>(() =>
    initialSeriesSelection.groupId ?? (session?.kind === 'task' ? session.seriesGroupId ?? null : null))
  const [selectedPageId, setSelectedPageId] = useState<string | null>(() =>
    initialSeriesSelection.pageId ?? (session?.kind === 'task' ? session.seriesPageId ?? null : null))
  const [seriesSubmitting, setSeriesSubmitting] = useState(false)
  const [seriesAction, setSeriesAction] = useState<string | null>(null)
  const modeRef = useRef(mode)
  useEffect(() => { modeRef.current = mode }, [mode])
  const seriesMode = seriesEnabled && mode === 'series'
  const seriesSelection = seriesMode ? resolveSeriesSelection(series, selectedGroupId, selectedPageId) : null
  const seriesBusy = seriesSubmitting || seriesAction !== null
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
  // Empty brand data must not lock the studio: the preset style degrades to "off" until one exists.
  const brandPresetUnavailable = presetStyleUnavailable(brandMode, {
    loading: brandsLoading,
    error: brandsError,
    matches: brandsForPlatform.length,
  })
  const effectiveBrandMode: BrandMode = brandPresetUnavailable ? 'none' : brandMode
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
  const selectedStoreId = (stores.find(store => store.place_id === storeSelection.placeId) ?? stores[0])?.place_id || ''
  const storeEnabled = storeSelection.enabled && Boolean(selectedStoreId)

  const logoOptions: BrandLogoImage[] = restoredLogo
    ? [...baseLogoOptions, restoredLogo]
    : baseLogoOptions
  const replacementLogoId = seriesSelection ? seriesSelection.group.logo_asset_id : effectiveLogoId
  const replacementLogoAsset = seriesSelection && replacementLogoId
    ? assets.find((asset) => asset.id === replacementLogoId) ?? assetFromTask(task, replacementLogoId)
    : null
  const replacementLogo = logoOptions.find((logo) => logo.asset_id === replacementLogoId)
    ?? (replacementLogoAsset ? logoFromAsset(replacementLogoAsset) : null)
  const selectedPlacement =
    selectedPlatform?.placements.find((item) => item.id === effectivePlacementId) ??
    selectedPlatform?.placements.find((item) => item.id === selectedPlatform.default_placement_id) ??
    null

  useEffect(() => {
    if (acceptedRef.current || creating) return
    const form: PosterDraftForm = {
      products, prompt, promptOptimized, selectedPlatformId, selectedPlacementId, selectedRatio,
      selectedBrandId, brandMode: effectiveBrandMode, selectedLogoId, logoChoiceTouched, styleReferences,
      storeSelection, selectedModel, modelCounts, mode,
    }
    const signature = JSON.stringify(form)
    if (initialFormRef.current === null) initialFormRef.current = signature
    const workspaceId = workspaceRef.current
    const blank = !draftStartedRef.current && signature === initialFormRef.current
    draftStartedRef.current = !blank
    const nextSession: PosterSession = blank
      ? { kind: 'blank', workspaceId, title, mode }
      : { kind: 'draft', workspaceId, title, form }
    setSessionSaveFailed(!savePosterSession(userId, merchantId, nextSession))
  }, [userId, merchantId, creating, title, products, prompt, promptOptimized, selectedPlatformId, selectedPlacementId,
    selectedRatio, selectedBrandId, effectiveBrandMode, selectedLogoId, logoChoiceTouched, styleReferences,
    storeSelection, selectedModel, modelCounts, mode])

  useEffect(() => {
    if (!acceptedRef.current || creating || !sessionRestored) return
    const seriesLink = seriesMode
      ? { groupId: seriesSelection?.group.id ?? selectedGroupId, pageId: seriesSelection?.page.page_id ?? selectedPageId }
      : null
    setSessionSaveFailed(!savePosterSession(userId, merchantId, {
      kind: 'task', taskId: task?.id ?? null, workspaceId: workspaceRef.current, selectedModel,
      ...(seriesMode
        ? { mode: 'series' as PosterSeriesMode, seriesGroupId: seriesLink?.groupId ?? null, seriesPageId: seriesLink?.pageId ?? null }
        : { mode: 'single' as PosterSeriesMode }),
    }))
    if (visible) updateWholeUrl(task?.id ?? null, workspaceRef.current, selectedModel, seriesLink)
  }, [visible, creating, sessionRestored, task?.id, selectedModel, userId, merchantId, seriesMode, seriesSelection, selectedGroupId, selectedPageId])

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
      brand_mode: effectiveBrandMode,
      brand_id: effectiveBrandMode === 'preset' ? effectiveBrandId : '',
    })
  }, [
    effectiveBrandMode,
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
    // A series workspace lists its pages through the series snapshot, not as poster candidates.
    if (session?.kind === 'task' && session.mode === 'series') setMode('series')
    const restore = async () => {
        const savedTask = taskId ? await getWholeImageTask(taskId) : null
        const workspace = savedTask?.input.generation_workspace_id ?? savedTask?.input.candidate_batch_id ?? savedTask?.id ?? savedWorkspace!
        // Task-center links carry only a page task id, so the series marker on the task decides.
        const isSeriesSession = (session?.kind === 'task' && session.mode === 'series') || Boolean(savedTask?.series)
        if (isSeriesSession) {
          setMode('series')
          const marker = savedTask?.series
          if (marker?.group_id) setSelectedGroupId(marker.group_id)
          if (marker?.page_id) setSelectedPageId(marker.page_id)
        }
        const [restoredCandidates, workspaceTitle, restoredSeries] = await Promise.all([
          isSeriesSession ? Promise.resolve([] as WholeImageTask[]) : listWholeImageWorkspace(workspace),
          getWholeImageWorkspaceTitle(workspace).catch(() => null),
          isSeriesSession ? getPosterSeriesWorkspace(workspace).catch(() => null) : null,
        ])
        if (cancelled) return
        workspaceRef.current = workspace
        setTitle(workspaceTitle?.title ?? formatPosterTitle(workspaceTitle?.created_at ??
          restoredCandidates[0]?.created_at ?? savedTask?.created_at ?? ''))
        const model = savedTask ? normalizeImageModel(savedTask.input.image_model) : selectedModelRef.current
        selectedModelRef.current = model
        setSelectedModel(model)
        const selected = savedTask ?? candidateForModel(restoredCandidates, model)
        setTask(selected)
        if (selected) modelSelections.current[model] = selected.id
        setCandidateTasks(restoredCandidates)
        setSessionRestored(true)
        setModelCounts(restoredCandidates.length ? restoreModelCounts(restoredCandidates) : defaultModelCounts())
        const restored = savedTask ?? restoredCandidates[0]
        const seriesGroupId = savedTask?.series?.group_id ?? selectionFromUrl(window.location.search).groupId
          ?? (session?.kind === 'task' ? session.seriesGroupId : null)
        const restoredGroup = restoredSeries?.groups.find(group => group.id === seriesGroupId)
          ?? restoredSeries?.groups[0]
        const groupProducts = seriesContentPool(restoredGroup ?? null).flatMap(item => {
          const asset = assetFromTask(restored ?? null, item.asset_id)
          if (!asset) return []
          return [{
            ...asset,
            original_name: item.name || asset.original_name,
            url: item.preview_url || asset.url,
            thumb_url: item.thumb_url || asset.thumb_url,
          }]
        })
        if (restoredSeries) setSeries(restoredSeries)
        if (!restored) {
          setProducts(groupProducts)
          return
        }
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
        const restoredProducts = restoredGroup?.content_pool !== undefined ? groupProducts : referenceIds
          .map((id) => assetFromTask(restored, id))
          .filter((asset): asset is Asset => Boolean(asset))
        setProducts(restoredProducts)
        setPrompt(isSeriesSession ? '' : hydrateImagePrompt(restored.input.prompt ?? '', restoredProducts.map(asset => ({ assetId: asset.id, name: asset.original_name, url: asset.url }))))
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
  const costWorkspaceId = workspaceId ?? initialWorkspaceId
  const submittingModels = Object.values(modelSubmissions).some(submission => submission && !submission.error)
  const workspaceRunning = submittingModels || candidateTasks.some(candidate => candidate.status === 'queued' || candidate.status === 'running')
  useEffect(() => {
    // Series pages are polled through the series snapshot; candidates belong to single mode.
    if (seriesMode || (!workspaceId && !candidateBatchId) || (!visible && !workspaceRunning)) {
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
  }, [workspaceId, candidateBatchId, visible, workspaceRunning, seriesMode])

  /* ------------------------------------------------------------------------------------
     Poster series: config, locked mode and the group/page snapshot
     ------------------------------------------------------------------------------------ */

  useEffect(() => {
    let cancelled = false
    void getPosterSeriesConfig()
      .then((config) => { if (!cancelled && mountedRef.current) setSeriesEnabled(Boolean(config.enabled)) })
      // A server without the feature must keep the single-poster workbench unchanged.
      .catch(() => { if (!cancelled) setSeriesEnabled(false) })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    if (!seriesEnabled || creating || !sessionRestored) return
    const workspaceId = workspaceRef.current
    if (!workspaceId) return
    let cancelled = false
    void getPosterWorkspaceMode(workspaceId).then((server) => {
      if (cancelled || !mountedRef.current) return
      const next = modeAfterServer({ mode: modeRef.current, locked: false }, server, modeRef.current)
      // A locked workspace (or one that already accepted a submission) owns its mode.
      setMode(server.locked || acceptedRef.current ? next.mode : modeRef.current)
      setModeLocked(Boolean(server.locked) || acceptedRef.current)
    }).catch(() => { /* mode stays local; the server still rejects a wrong-mode submit */ })
    return () => { cancelled = true }
  }, [seriesEnabled, creating, sessionRestored])

  const refreshSeries = useCallback(async (): Promise<PosterSeriesWorkspace | null> => {
    const workspaceId = workspaceRef.current
    if (!workspaceId) return null
    try {
      const next = await getPosterSeriesWorkspace(workspaceId)
      if (mountedRef.current) {
        setSeries(next)
        setModeLocked((previous) => previous || next.locked)
      }
      return next
    } catch (reason: unknown) {
      if (mountedRef.current) setError(errorMessage(reason))
      return null
    }
  }, [])

  useEffect(() => {
    if (!seriesMode || !sessionRestored) return
    const workspaceId = workspaceRef.current
    if (!workspaceId) return
    let cancelled = false
    void (async () => {
      try {
        const next = await getPosterSeriesWorkspace(workspaceId)
        if (cancelled || !mountedRef.current) return
        setSeries(next)
        setModeLocked((previous) => previous || next.locked)
      } catch (reason: unknown) {
        if (!cancelled && mountedRef.current) setError(errorMessage(reason))
      }
    })()
    return () => { cancelled = true }
  }, [seriesMode, sessionRestored])

  /** Re-read the page task that is bound to the preview after a retry or an interiors start. */
  const reloadBoundPageTask = useCallback(async () => {
    const taskId = boundPageTaskRef.current
    if (!taskId) return
    try {
      const next = await getWholeImageTask(taskId)
      if (!mountedRef.current || boundPageTaskRef.current !== next.id) return
      setTask(next)
      if (next.current_version_id !== lastVersionRef.current) {
        lastVersionRef.current = next.current_version_id
        await loadVersions(next.id)
      }
    } catch { /* the next poll retries */ }
  }, [loadVersions])

  const seriesPolling = seriesMode && (seriesNeedsPoll(series) || seriesBusy)
  useEffect(() => {
    if (!seriesPolling) return
    let cancelled = false
    const tick = async () => {
      const workspaceId = workspaceRef.current
      if (!workspaceId) return
      try {
        const next = await getPosterSeriesWorkspace(workspaceId)
        if (cancelled || !mountedRef.current) return
        setSeries(next)
        // A page that just restarted keeps its task id, so the preview has to be re-read.
        const bound = boundPageTaskRef.current
        if (bound && next.groups.some((group) => group.pages.some((page) =>
          page.whole_task_id === bound && (page.status === 'queued' || page.status === 'running')))) {
          await reloadBoundPageTask()
        }
      } catch { /* a transient poll failure keeps the last snapshot */ }
    }
    const interval = window.setInterval(() => void tick(), 2000)
    return () => { cancelled = true; window.clearInterval(interval) }
  }, [seriesPolling, reloadBoundPageTask])

  // A restored draft/round shows the requirement the round was submitted with.
  useEffect(() => {
    if (!seriesMode) return
    const run = series?.runs.at(-1)
    if (run?.prompt) setPrompt((previous) => previous.trim() ? previous : run.prompt)
  }, [seriesMode, series])

  /*
    Every series page is an ordinary whole-image task, so selecting a page binds the same
    preview, chat edit, replacement and version components to that page's task.
  */
  const boundPageTaskRef = useRef<string | null>(null)
  const selectedPageTaskId = seriesSelection?.page.whole_task_id ?? null
  useEffect(() => {
    if (!seriesMode) {
      boundPageTaskRef.current = null
      return
    }
    if (!seriesSelection) return
    if (boundPageTaskRef.current === selectedPageTaskId) return
    boundPageTaskRef.current = selectedPageTaskId
    if (!selectedPageTaskId) {
      setTask(null)
      setVersions([])
      lastVersionRef.current = null
      ++versionRequestRef.current
      setVersionsLoading(false)
      return
    }
    let cancelled = false
    void getWholeImageTask(selectedPageTaskId).then((next) => {
      if (cancelled || !mountedRef.current || boundPageTaskRef.current !== selectedPageTaskId) return
      setTask(next)
      setCandidateTasks([])
      lastVersionRef.current = next.current_version_id
      if (next.current_version > 0) void loadVersions(next.id)
    }).catch((reason: unknown) => {
      if (!cancelled && mountedRef.current) setError(errorMessage(reason))
    })
    return () => { cancelled = true }
    // Only the page task id may re-run this: the selection object is rebuilt every render, and
    // depending on it would cancel the task fetch before it can bind the page.
  }, [seriesMode, selectedPageTaskId, loadVersions])


  const visibleCandidates = mergeCandidates(candidateTasks, task ? [task] : [])
  const taskId = task?.id ?? null
  const taskStatus = task?.status ?? null
  const editRunning = task?.edit.status === 'running'
  const chatMessages = task?.messages ?? []
  // The history tab only exists while there is something to review.
  const visibleTabs = TABS.filter(tab => tab.key !== 'chat_history' || chatMessages.length > 0 || editRunning)
  // `auto` opens the history as soon as there is one, until the operator closes it by hand.
  const resolvedTab: StudioTab | null = studioTab === 'auto'
    ? (chatMessages.length || editRunning ? 'chat_history' : null)
    : studioTab
  const activeTab = visibleTabs.some(tab => tab.key === resolvedTab) ? resolvedTab : null

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
          if (seriesMode) void refreshSeries()
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
  }, [taskId, taskStatus, editRunning, loadVersions, seriesMode, refreshSeries])

  const active = Boolean(task && (task.status === 'queued' || task.status === 'running'))
  const busy = creating || active || editRunning || productsUploading || selectingOutput || seriesBusy
  const formBusy = creating || productsUploading || styleReferencesUploading || seriesBusy
  const promptImages: PromptImage[] = products.map(asset => ({ assetId: asset.id, name: asset.original_name, url: asset.url }))
  // Every unmet condition is listed once, so the disabled button can explain itself instead of
  // failing silently.
  const blockers = generationBlockers({
    promptImages,
    prompt,
    brandMode: effectiveBrandMode,
    brandsLoading,
    brandsError,
    platformResolved,
    platformReady: Boolean(selectedPlatform && selectedPlacement),
    uploading: formBusy,
  })
  const generationDisabled = blockers.length > 0
  const canEdit = Boolean(task?.status === 'succeeded' && task.current_version_id && task.current_output_id)
  const currentOutput = task?.outputs.find(output => output.id === task.current_output_id) ?? task?.outputs[0]
  const currentPreview = currentOutput?.preview_url ?? task?.preview_url
  // The download entry sits in the composer footer, so the studio resolves the URL for it.
  const downloadHref = currentOutput?.download_url ?? task?.download_url ?? null
  const currentPromptImage: PromptImage | null = task && currentPreview ? {
    assetId: `current:${task.id}:${task.current_version_id}:${task.current_output_id}`,
    name: '当前海报', url: currentPreview, removable: false,
  } : null

  /*
    Original-photo replacement on a series page defaults to the current version's content
    references, and offers the rest of this task's uploaded pool as alternative sources.
  */
  const seriesPoolAssets = seriesMode && seriesSelection
    ? assets.filter((asset) => replacementPool(
        seriesSelection.page,
        taskAssetPool(series, seriesSelection.group, products.map((item) => item.id)),
      ).includes(asset.id))
    : []

  const lockWorkspaceMode = (savedMode: PosterSeriesMode) => {
    acceptedRef.current = true
    modeRef.current = savedMode
    setMode(savedMode)
    setModeLocked(true)
  }

  const rememberAcceptedTask = (candidate: WholeImageTask | null) => {
    if (!mountedRef.current) return
    lockWorkspaceMode('single')
    const model = selectedModelRef.current
    const selectedId = modelSelections.current[model] ??
      (candidate && normalizeImageModel(candidate.input.image_model) === model ? candidate.id : null)
    setSessionSaveFailed(!savePosterSession(userId, merchantId, {
      kind: 'task', taskId: selectedId, workspaceId: workspaceRef.current, selectedModel: model,
      mode: modeRef.current, seriesGroupId: null, seriesPageId: null,
    }))
  }

  const saveTitle = async () => {
    const next = titleEdit?.trim()
    if (!next || savingTitle || modelLocks.current.size) return
    if (next === title) {
      setTitleEdit(null)
      return
    }
    if (!acceptedRef.current) {
      setTitle(next)
      setTitleEdit(null)
      return
    }
    setSavingTitle(true)
    try {
      const saved = await renameWholeImageWorkspace(workspaceRef.current, next)
      setTitle(saved.title ?? next)
      setTitleEdit(null)
    } catch (reason: unknown) {
      setError(errorMessage(reason))
    } finally {
      setSavingTitle(false)
    }
  }

  const handleGenerate = async (onlyModel?: ImageModel) => {
    if (formBusy || seriesBusy) {
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
    if (brandMode === 'preset' && !brandPresetUnavailable &&
      !brandsForPlatform.some((brand) => brand.id === effectiveBrandId)) {
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
    // The series workbench plans groups before it creates any page task, and one click
    // submits every model that has a group count instead of only the viewed model.
    if (seriesEnabled && mode === 'series') {
      await submitSeriesRound()
      return
    }
    const models = IMAGE_MODEL_OPTIONS.filter(option =>
      (!onlyModel || option.value === onlyModel) && (modelCounts[option.value] ?? 0) > 0 &&
      !modelLocks.current.has(option.value) && !modelGenerationState(visibleCandidates, option.value).running)
    const payload = {
            prompt: submittedPrompt,
            workspace_title: acceptedRef.current ? undefined : title,
            prompt_optimized: promptOptimized,
            brand_id: effectiveBrandMode === 'preset' ? effectiveBrandId : null,
            brand_mode: effectiveBrandMode,
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
      const previousTaskId = readWholeTaskId()
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
        if (!created.length && result.errors.some(isWorkspaceModeConflict)) {
          lockWorkspaceMode('series')
          setError(null)
        }
        const failures = result.errors.filter(reason => !isWorkspaceModeConflict(reason))
        if (failures.length) {
          // Reconcile a response lost after insertion before offering a manual new attempt.
          try { created = mergeCandidates(created, await listWholeImageWorkspace(workspaceRef.current)) } catch { /* no confirmed tasks yet */ }
          if (!mountedRef.current) return
          const confirmed = created.filter(item => item.input.candidate_batch_id === batchId).length
          if (confirmed < count) submissionError = `${count - confirmed} 张提交未确认，请先核对任务中心再手动重试。${errorMessage(failures[0])}`
        }
        created = created.filter(item => !deletedCandidates.current.has(item.id))
        setCandidateTasks(previous => mergeCandidates(previous, created).filter(item => !deletedCandidates.current.has(item.id)))
        const batch = candidateRounds(created.filter(item => item.input.candidate_batch_id === batchId))[0]
        const nextForModel = batch ? candidateForModel(batch.candidates, model) : null
        if (nextForModel && selectedModelRef.current === model && !editRunning &&
          readWholeTaskId() === previousTaskId) {
          handleSelectCandidate(nextForModel)
        }
        if (created.length) rememberAcceptedTask(nextForModel)
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

  const seriesGroupTotal = IMAGE_MODEL_OPTIONS.reduce((total, option) =>
    total + Math.max(0, modelCounts[option.value] ?? 0), 0)
  const seriesSubmitError = Object.values(modelSubmissions).find(submission => submission?.error)?.error ?? null

  const seriesGroupPlan = (onlyModel?: ImageModel) => IMAGE_MODEL_OPTIONS
    .filter((option) => (!onlyModel || option.value === onlyModel) && (modelCounts[option.value] ?? 0) > 0)
    .map((option) => ({ image_model: option.value, group_count: modelCounts[option.value] ?? 0 }))

  /**
   * One series submission creates one round. The model count is a group count.
   */
  const submitSeriesRound = async (onlyModel?: ImageModel) => {
    const groups = seriesGroupPlan(onlyModel)
    if (!groups.length) {
      setError('请选择每个模型的组数')
      return
    }
    let submittedPrompt: string
    try { submittedPrompt = serializeImagePrompt(prompt, promptImages) }
    catch (reason: unknown) { setError(errorMessage(reason)); return }
    const submitId = crypto.randomUUID()
    const models = groups.map((group) => group.image_model)
    const previousGroups = new Set((series?.groups ?? []).map((group) => group.id))
    models.forEach((model) => modelLocks.current.add(model))
    setSeriesSubmitting(true)
    setError(null)
    setModelSubmissions((previous) => {
      const next = { ...previous }
      models.forEach((model) => { next[model] = { batchId: submitId, count: modelCounts[model] ?? 0 } })
      return next
    })
    try {
      const payload = buildSeriesRunPayload({
        submitId,
        prompt: submittedPrompt,
        promptOptimized,
        ratio: selectedPlatform ? undefined : selectedRatio,
        products: products.map((asset) => asset.id),
        brandId: effectiveBrandMode === 'preset' ? effectiveBrandId : null,
        brandMode: effectiveBrandMode,
        logoAssetId: effectiveLogoId || null,
        styleReferenceIds: styleReferences.map(asset => asset.id),
        modelCounts: Object.fromEntries(groups.map((group) => [group.image_model, group.group_count])),
        title: acceptedRef.current ? undefined : title,
        platformId: selectedPlatform?.id ?? null,
        placementId: selectedPlacement?.id ?? null,
        includeStoreAddress: storeEnabled && storeSelection.address,
        includeStorePhone: storeEnabled && storeSelection.phone,
        storePlaceId: storeEnabled ? selectedStoreId : null,
      })
      const next = await createPosterSeriesRun(workspaceRef.current, payload)
      if (!mountedRef.current) return
      setSeries(next)
      lockWorkspaceMode('series')
      const created = next.groups.filter((group) => !previousGroups.has(group.id))
      const group = created[0] ?? next.groups.at(-1) ?? null
      const page = group ? defaultGroupPage(group) : null
      if (group) handleSelectSeriesPage(group, page)
      setSessionSaveFailed(!savePosterSession(userId, merchantId, {
        kind: 'task', taskId: null, workspaceId: workspaceRef.current,
        selectedModel: group?.image_model ?? selectedModelRef.current, mode: 'series',
        seriesGroupId: group?.id ?? null, seriesPageId: page?.page_id ?? null,
      }))
    } catch (reason: unknown) {
      if (isWorkspaceModeConflict(reason)) {
        lockWorkspaceMode('single')
        setError(null)
        void listWholeImageWorkspace(workspaceRef.current).then(candidates => {
          if (mountedRef.current) setCandidateTasks(candidates)
        }).catch(() => { /* The saved workspace mode is already known. */ })
      } else {
        setError(errorMessage(reason))
      }
    } finally {
      models.forEach((model) => modelLocks.current.delete(model))
      setModelSubmissions((previous) => {
        const next = { ...previous }
        models.forEach((model) => { delete next[model] })
        return next
      })
      if (mountedRef.current) setSeriesSubmitting(false)
    }
  }

  const handleSeriesModeChange = (next: PosterSeriesMode) => {
    if (modeLocked || formBusy || modelLocks.current.size) return
    setMode(next)
    if (next !== 'series') {
      setSelectedGroupId(null)
      setSelectedPageId(null)
      setSeries(null)
      acceptedRef.current = false
      boundPageTaskRef.current = null
    }
  }

  const handleSelectSeriesPage = (group: PosterSeriesGroup, page: PosterSeriesPage | null) => {
    if (!page || page.whole_task_id !== boundPageTaskRef.current) {
      boundPageTaskRef.current = null
      setTask(null)
      setVersions([])
      ++versionRequestRef.current
      setVersionsLoading(false)
    }
    setSelectedGroupId(group.id)
    setSelectedPageId(page?.page_id ?? null)
    // Switching models in the toolbar switches to that model's group, so keep them in sync.
    const model = normalizeImageModel(group.image_model)
    if (IMAGE_MODEL_OPTIONS.some((option) => option.value === model)) {
      selectedModelRef.current = model
      setSelectedModel(model)
    }
  }

  const handleSelectSeriesModel = (model: ImageModel) => {
    if (seriesBusy) return
    selectedModelRef.current = model
    setSelectedModel(model)
    const group = series?.groups.find((item) => normalizeImageModel(item.image_model) === model)
    const page = group ? defaultGroupPage(group) : null
    if (group) handleSelectSeriesPage(group, page)
  }

  const handleRetrySeriesPage = async (group: PosterSeriesGroup, page: PosterSeriesPage) => {
    if (seriesBusy) return
    setSeriesAction(page.page_id)
    setError(null)
    try {
      const updated = await retryPosterSeriesPage(group.id, page.page_id)
      if (!mountedRef.current) return
      setSeries((previous) => mergeSeriesPage(previous, updated))
      await Promise.all([refreshSeries(), reloadBoundPageTask()])
    } catch (reason: unknown) {
      setError(errorMessage(reason))
    } finally {
      if (mountedRef.current) setSeriesAction(null)
    }
  }

  /** "Save to this page": the isolated editor canvas becomes a new version of this page. */
  const handleSaveSeriesHtml = async (input: {
    sourceVersionId: string
    expectedOutputId: string | null
    artifactId: string
    sessionId: string
    base64: string
    width: number
    height: number
  }) => {
    if (!seriesSelection) throw new Error('请先选择一页')
    const { group, page } = seriesSelection
    const body = htmlSaveBody({
      sessionId: input.sessionId,
      sourceVersionId: input.sourceVersionId,
      expectedOutputId: input.expectedOutputId,
      artifactId: input.artifactId,
      base64: input.base64,
      width: input.width,
      height: input.height,
      contentRefs: replacementDefaults(page),
    })
    const updated = await savePosterSeriesHtmlPage(group.id, page.page_id, body)
    if (mountedRef.current) setSeries((previous) => mergeSeriesPage(previous, updated))
    const taskId = updated.whole_task_id ?? page.whole_task_id
    if (taskId) {
      const next = await getWholeImageTask(taskId)
      if (!mountedRef.current) return
      // The operator may have switched pages while the save ran: only update the page
      // that is still bound to the preview, and never steal the selection.
      if (boundPageTaskRef.current === taskId) {
        setTask(next)
        lastVersionRef.current = next.current_version_id
        await loadVersions(next.id)
      }
    }
    await refreshSeries()
  }

  const handleEdit = async (
    instruction: string,
    sourceVersionId = task?.current_version_id ?? null,
    referenceAssetIds: string[] = [],
    imageModel?: ImageModel,
  ): Promise<boolean> => {
    if (!task || !sourceVersionId || editRunning || submitLock.current || deleteLock.current) {
      return false
    }
    const editedTaskId = task.id
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
        imageModel,
      )
      // A series edit belongs to the page it was started on; a late response must not
      // replace the page the operator has since opened.
      if (seriesMode && boundPageTaskRef.current !== editedTaskId) return true
      setTask(next)
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

  const handleSelectVersion = async (versionId: string): Promise<boolean> => {
    if (!task || busy || submitLock.current || deleteLock.current) return false
    if (versionId === task.current_version_id) return true
    submitLock.current = true
    setSelectingOutput(true)
    setError(null)
    try {
      const next = await selectWholeImageVersion(task.id, versionId)
      setTask(previous => previous?.id === next.id ? next : previous)
      setCandidateTasks(previous => mergeCandidates(previous, [next]))
      if (readWholeTaskId() === next.id) lastVersionRef.current = next.current_version_id
      if (seriesMode) void refreshSeries()
      return true
    } catch (reason: unknown) {
      setError(errorMessage(reason))
      return false
    } finally {
      submitLock.current = false
      setSelectingOutput(false)
    }
  }

  const handleSelectOutput = async (versionId: string, outputId: string) => {
    if (!task || selectingOutput) return
    setSelectingOutput(true)
    setError(null)
    try {
      const next = await selectWholeImageOutput(task.id, versionId, outputId)
      setTask(previous => previous?.id === next.id ? next : previous)
      if (task.id === boundPageTaskRef.current || !seriesMode) lastVersionRef.current = next.current_version_id
      if (seriesMode) void refreshSeries()
    } catch (reason: unknown) {
      setError(errorMessage(reason))
    } finally {
      setSelectingOutput(false)
    }
  }

  const handleSelectCandidate = (candidate: WholeImageTask) => {
    // Series pages are selected by group/page, never as poster candidates.
    if (seriesMode) return
    if (editRunning || submitLock.current || selectingOutput || deleteLock.current) return
    const model = normalizeImageModel(candidate.input.image_model)
    selectedModelRef.current = model
    setSelectedModel(model)
    modelSelections.current[model] = candidate.id
    setTask(candidate)
    setVersions([])
    ++versionRequestRef.current
    setVersionsLoading(false)
    lastVersionRef.current = candidate.current_version_id
    if (visibleRef.current) updateWholeUrl(candidate.id, workspaceRef.current, model)
    if (candidate.current_version > 0) void loadVersions(candidate.id)
  }

  const handleSelectModel = (model: ImageModel) => {
    if (creating || editRunning || submitLock.current || selectingOutput || deleteLock.current || model === selectedModelRef.current) return
    selectedModelRef.current = model
    setSelectedModel(model)
    const remembered = visibleCandidates.find(candidate => candidate.id === modelSelections.current[model])
    const latest = candidateRounds(visibleCandidates).findLast(round => round.model === model)
    const next = remembered ?? candidateForModel(latest?.candidates ?? [], model)
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
    if (seriesMode) return
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
        const model = selectedModelRef.current
        const rounds = candidateRounds(remaining)
        const siblings = rounds.find(round => round.id === (candidate.input.candidate_batch_id ?? candidate.id))
        const next = candidateForModel((siblings ?? rounds.findLast(round => round.model === model))?.candidates ?? [], model)
        setTask(next)
        setVersions([])
        ++versionRequestRef.current
        setVersionsLoading(false)
        lastVersionRef.current = next?.current_version_id ?? null
        if (next) modelSelections.current[model] = next.id
        updateWholeUrl(next?.id ?? null, workspaceRef.current, model)
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
    if (!files.length) return
    setProductsUploading(true)
    setError(null)
    try {
      // Uploads land in the asset library, so the picker and later posters can reuse them.
      const uploaded = await Promise.all(files.map((file) => uploadAsset(file)))
      setProducts((previous) => [...new Map([...previous, ...uploaded].map(asset => [asset.id, asset])).values()])
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

  const selectUtility = (next: StudioTab | null) => {
    setStudioTab(next)
    const url = new URL(window.location.href)
    if (next === 'original_replace') url.searchParams.set('tab', 'original_replace')
    else url.searchParams.delete('tab')
    window.history.replaceState({}, '', url)
  }

  const selectedGroup = seriesSelection?.group ?? series?.groups.find(group => group.id === selectedGroupId) ?? series?.groups[0]
  const selectedPage = seriesSelection?.page
  const groupLoading = Boolean(selectedGroup && !selectedPage && ['queued', 'running'].includes(selectedGroup.status))
  const roundById = new Map(series?.runs.map(run => [run.id, run.round]))
  const imageBatches: PosterImageBatch[] = seriesMode
    ? (series?.groups ?? []).map(group => ({
        id: group.id,
        round: roundById.get(group.run_id) ?? 1,
        model: imageModelLabel(group.image_model),
        group: `组 ${group.order + 1}`,
        status: group.pages.length ? undefined : seriesStatusLabel(group.status),
        onSelectEmpty: () => handleSelectSeriesPage(group, null),
        images: group.pages.map(page => ({
          id: page.page_id,
          label: seriesPageLabel(page),
          thumb: (task && task.id === page.whole_task_id ? task.thumb_url || task.preview_url : null)
            || page.thumb_url || page.preview_url,
          status: page.status === 'succeeded' ? undefined : seriesStatusLabel(page.status),
          error: page.error,
          onSelect: () => handleSelectSeriesPage(group, page),
          retry: page.status === 'failed' ? {
            onClick: () => void handleRetrySeriesPage(group, page), disabled: seriesBusy,
            pending: seriesAction === page.page_id,
          } : undefined,
        })),
      }))
    : candidateRounds(visibleCandidates).map(round => ({
        id: round.id,
        round: round.round,
        model: imageModelLabel(round.model),
        images: round.candidates.map((candidate, index) => ({
          id: candidate.id,
          label: `图片 ${(candidate.input.candidate_index ?? index) + 1}`,
          thumb: candidate.thumb_url || candidate.preview_url,
          status: candidate.status === 'succeeded' ? undefined : seriesStatusLabel(candidate.status),
          onSelect: () => handleSelectCandidate(candidate),
          remove: {
            onClick: () => handleDeleteCandidate(candidate),
            disabled: deletingCandidateId !== null || candidate.status === 'queued' ||
              candidate.status === 'running' || candidate.edit.status === 'running',
            pending: deletingCandidateId === candidate.id,
          },
        })),
      }))
  return (
    <main className="workspace poster-workspace has-studio">
      <WholeImageForm
        workspaceId={costWorkspaceId}
        brands={brandsForPlatform}
        selectedBrandId={effectiveBrandId}
        brandsLoading={brandsLoading}
        brandsError={brandsError}
        brandMode={effectiveBrandMode}
        brandPresetUnavailable={brandPresetUnavailable}
        brandStudioHref={brandStudioHref}
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
        seriesEnabled={seriesEnabled}
        mode={mode}
        modeLocked={modeLocked || submittingModels}
        onModeChange={handleSeriesModeChange}
      >
        <ImageModelSelector
          counts={modelCounts}
          tasks={seriesMode ? [] : visibleCandidates}
          submissions={modelSubmissions}
          disabled={generationDisabled || seriesBusy}
          disabledReasons={blockers}
          onChange={(model, count) => setModelCounts(previous => ({ ...previous, [model]: count }))}
          onGenerate={(model) => void handleGenerate(model)}
          selectedModel={selectedModel}
          selectionDisabled={creating || editRunning || selectingOutput || deletingCandidateId !== null || seriesBusy}
          onSelectModel={seriesMode ? handleSelectSeriesModel : handleSelectModel}
          countUnit={seriesMode ? '组' : '张'}
          countNoun={seriesMode ? '生成组数' : '生成张数'}
          seriesSubmit={seriesMode ? {
            total: seriesGroupTotal,
            busy: seriesSubmitting,
            error: seriesSubmitError,
            onGenerate: () => void handleGenerate(),
          } : undefined}
        />
      </WholeImageForm>

      <div className={`poster-editor${activeTab ? ' has-utility-panel' : ''}`}>
        <div className="poster-utility-toolbar">
          {titleEdit === null ? (
            <div className="poster-task-heading">
              <h1 title={title}>{title || '海报任务'}</h1>
              {seriesMode ? <span className="mp-badge poster-mode-badge">系列图</span> : null}
              <button type="button" className="mp-btn-ghost poster-title-edit" aria-label="编辑任务标题" title="编辑任务标题"
                disabled={!title || creating || submittingModels} onClick={() => setTitleEdit(title)}>
                <span aria-hidden="true">✎</span>
              </button>
            </div>
          ) : (
            <form className="poster-task-heading" onSubmit={(event) => { event.preventDefault(); void saveTitle() }}
              onKeyDown={(event) => { if (event.key === 'Escape' && !savingTitle) setTitleEdit(null) }}>
              <input className="mp-input poster-task-title-input" aria-label="任务标题" autoFocus required maxLength={100}
                value={titleEdit} disabled={savingTitle} onChange={(event) => setTitleEdit(event.target.value)} />
              <button className="mp-btn-secondary mp-btn-small" disabled={!titleEdit.trim() || savingTitle || submittingModels}>
                {savingTitle ? '保存中…' : '保存'}
              </button>
              <button type="button" className="mp-btn-ghost mp-btn-small" disabled={savingTitle} onClick={() => setTitleEdit(null)}>取消</button>
            </form>
          )}
          <a className="mp-btn-secondary mp-btn-small" href={newDesignHref}
            target="_blank" rel="noopener noreferrer" title="在新标签页打开，保留当前设计">
            ＋ 新建海报
          </a>
          <div className="model-toolbar-tools" aria-label="辅助工具">
            {visibleTabs.map(tab => (
              <button key={tab.key} type="button" aria-expanded={activeTab === tab.key}
                aria-controls="studio-utility-panel" className={activeTab === tab.key ? 'is-active' : ''}
                onClick={() => selectUtility(activeTab === tab.key ? null : tab.key)}>
                {tab.label}
                {tab.key === 'chat_history' && chatMessages.length ? (
                  <span className="tab-count">{chatMessages.length}</span>
                ) : null}
              </button>
            ))}
          </div>
        </div>
        <WholeImageResultPanel
          task={task}
          busy={creating}
          loading={groupLoading || Boolean(selectedPage?.whole_task_id && !task && selectedPage.status !== 'failed')}
          emptyMessage={!selectedPage ? selectedGroup?.error ?? undefined : seriesStatusLabel(selectedPage.status)}
          onRetry={(candidate) => {
            if (selectedGroup && selectedPage?.status === 'failed' && candidate.status === 'failed') {
              void handleRetrySeriesPage(selectedGroup, selectedPage)
            } else {
              void handleRetry(candidate)
            }
          }}
          onSelectOutput={(versionId, outputId) => void handleSelectOutput(versionId, outputId)}
          selectingOutput={selectingOutput}
          navigation={imageBatches.length ? (
            <PosterImageNavigator batches={imageBatches}
              activeBatchId={seriesMode ? selectedGroup?.id ?? null : task?.input.candidate_batch_id ?? task?.id ?? null}
              selectedId={seriesMode ? selectedPage?.page_id ?? null : task?.id ?? null}
              disabled={!visible || (!seriesMode && editRunning) || selectingOutput || deletingCandidateId !== null || seriesBusy}
              versions={versions} versionsLoading={versionsLoading} currentVersionId={task?.current_version_id ?? null}
              versionSelectionDisabled={busy} onSelectVersion={handleSelectVersion} />
          ) : null}
        >
          <WholeChatPanel
            key={task?.id}
            initialModel={normalizeImageModel(task?.edit.image_model ?? task?.model_input?.model ?? task?.input.image_model ?? selectedModel) as ImageModel}
            edit={task?.edit ?? { status: 'idle' }}
            disabled={!canEdit || busy}
            onUpload={handleUploadChatImages}
            downloadHref={downloadHref}
            finalizationActions={task?.current_version_id && task.current_output_id ? (
              <PosterFinalizationControl key={`${task.id}:${task.current_version_id}:${task.current_output_id}`} task={task} disabled={busy} />
            ) : null}
            currentImage={currentPromptImage}
            assets={assets}
            assetsLoading={assetsLoading}
            onLoadAssets={onLoadAssets}
            onGoAssets={onGoAssets}
            onSend={(instruction, attachments, imageModel) =>
              handleEdit(
                instruction,
                task?.current_version_id ?? null,
                attachments.map((asset) => asset.id),
                imageModel,
              )
            }
          />
        </WholeImageResultPanel>

        {activeTab ? (
          <aside id="studio-utility-panel" className={`studio-inspector${activeTab === 'original_replace' ? ' original-replacement-inspector' : ''}`} aria-label="辅助工具内容">
            <div className="studio-utility-heading">
              <button type="button" aria-label="收起辅助工具" onClick={() => selectUtility(null)}>×</button>
            </div>
            <div className="studio-tab-body">
              {activeTab === 'chat_history' ? (
                <WholeChatHistory messages={chatMessages} versions={versions} edit={task?.edit ?? { status: 'idle' }} />
              ) : null}
              {activeTab === 'original_replace' && task ? (
                <OriginalReplacementPanel key={task.id} task={task} assets={assets}
                  logo={replacementLogo}
                  initialPhotos={seriesMode && seriesSelection ? replacementDefaults(seriesSelection.page) : undefined}
                  poolAssets={seriesMode && seriesSelection ? seriesPoolAssets : undefined}
                  seriesCanvas={seriesMode && seriesSelection ? seriesSelection.group.canvas : null}
                  onSaveSeriesHtml={seriesMode && seriesSelection ? handleSaveSeriesHtml : undefined}
                  onRefreshTask={async () => {
                    // A series page may have been switched while the panel was open, so the
                    // refresh follows the page bound to the preview instead of this closure.
                    if (seriesMode) {
                      await Promise.all([reloadBoundPageTask(), refreshSeries()])
                      return
                    }
                    const next = await getWholeImageTask(task.id)
                    setTask(next)
                    lastVersionRef.current = next.current_version_id
                    await loadVersions(next.id)
                  }} />
              ) : null}
              {activeTab === 'costs' ? (
                <PosterCostPanel key={costWorkspaceId} workspaceId={costWorkspaceId} />
              ) : null}
              {activeTab === 'model_input' ? (
                <WholeModelInputPanel
                  modelInput={task?.model_input ?? null}
                  outputs={task?.outputs ?? []}
                  branches={task?.edit.branches}
                />
              ) : null}
            </div>
          </aside>
        ) : null}
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
        max={null}
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
