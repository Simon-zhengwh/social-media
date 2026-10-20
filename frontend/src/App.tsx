import { useCallback, useEffect, useRef, useState } from 'react'
import AssetLibrary from './components/AssetLibrary'
import BrandStudio from './components/BrandStudio'
import AssetPicker from './components/AssetPicker'
import ChatPanel from './components/ChatPanel'
import GenerateForm from './components/GenerateForm'
import LayerPanel from './components/LayerPanel'
import ResultPanel from './components/ResultPanel'
import Sidebar, { type AppView } from './components/Sidebar'
import VersionPanel from './components/VersionPanel'
import WholeImageStudio from './components/WholeImageStudio'
import {
  ApiError,
  createEdit,
  createTask,
  deleteAsset,
  getSamples,
  getTask,
  listAssets,
  listBrands,
  listPlatforms,
  listTaskBatch,
  listVersions,
  patchLayer,
  regenerateComponent,
  resumeTask,
  restoreVersion,
  uploadAsset,
} from './api'
import type {
  Asset,
  AssetKind,
  BrandMode,
  BrandStyle,
  LayerPatch,
  MerchantInfo,
  PlatformPreset,
  Task,
  VersionSummary,
} from './types'

type StudioTab = 'chat' | 'layers' | 'versions'

function readTaskId(): string | null {
  return new URLSearchParams(window.location.search).get('task')
}

function readView(): AppView {
  const value = new URLSearchParams(window.location.search).get('view')
  if (value === 'assets') {
    return 'assets'
  }
  if (value === 'brands') {
    return 'brands'
  }
  if (value === 'whole') {
    return 'whole'
  }
  return 'poster'
}

function updateUrl(next: { view?: AppView; taskId?: string | null }) {
  const url = new URL(window.location.href)
  if (next.view !== undefined) {
    if (next.view === 'poster') {
      url.searchParams.delete('view')
    } else {
      url.searchParams.set('view', next.view)
    }
  }
  if (next.taskId !== undefined) {
    if (next.taskId) {
      url.searchParams.set('task', next.taskId)
    } else {
      url.searchParams.delete('task')
    }
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
  task: Task,
  kind: AssetKind,
  assetId?: string | null,
): Asset | null {
  const id = assetId ?? task.input.product_asset_id
  if (!id) {
    return null
  }
  return {
    id,
    kind,
    original_name: '已保存的参考素材',
    width: 0,
    height: 0,
    mime_type: 'image/png',
    url: `/api/assets/${id}/file`,
    created_at: task.created_at,
  }
}

const TABS: Array<{ key: StudioTab; label: string }> = [
  { key: 'chat', label: '对话' },
  { key: 'layers', label: '图层' },
  { key: 'versions', label: '版本' },
]

export default function App() {
  const [view, setView] = useState<AppView>(() => readView())
  const [products, setProducts] = useState<Asset[]>([])
  const [brands, setBrands] = useState<BrandStyle[]>([])
  const [brandsLoading, setBrandsLoading] = useState(true)
  const [brandsError, setBrandsError] = useState<string | null>(null)
  const [selectedBrandId, setSelectedBrandId] = useState('')
  const [brandMode, setBrandMode] = useState<BrandMode>('preset')
  const [styleReferences, setStyleReferences] = useState<Asset[]>([])
  const [styleReferencesUploading, setStyleReferencesUploading] = useState(false)
  const [platforms, setPlatforms] = useState<PlatformPreset[]>([])
  const [platformsLoading, setPlatformsLoading] = useState(true)
  const [platformsError, setPlatformsError] = useState<string | null>(null)
  const [selectedPlatformId, setSelectedPlatformId] = useState('')
  const [selectedPlacementId, setSelectedPlacementId] = useState('')
  const [merchantInfo, setMerchantInfo] = useState<MerchantInfo>({ address: '', phone: '' })
  const [prompt, setPrompt] = useState('')
  const [candidateCount, setCandidateCount] = useState(1)
  const [candidateTasks, setCandidateTasks] = useState<Task[]>([])
  const [task, setTask] = useState<Task | null>(null)
  const [busy, setBusy] = useState(() => Boolean(readTaskId()))
  const [error, setError] = useState<string | null>(null)
  const [assets, setAssets] = useState<Asset[]>([])
  const [assetsLoading, setAssetsLoading] = useState(true)
  const [assetsError, setAssetsError] = useState<string | null>(null)
  const [uploadingKind, setUploadingKind] = useState<AssetKind | null>(null)
  const [pickerKind, setPickerKind] = useState<AssetKind | null>(null)
  const [selectedLayerId, setSelectedLayerId] = useState<string | null>(null)
  const [versions, setVersions] = useState<VersionSummary[]>([])
  const [versionsLoading, setVersionsLoading] = useState(false)
  const [studioTab, setStudioTab] = useState<StudioTab>('chat')
  const pollRef = useRef<number | null>(null)
  const inFlight = useRef(false)
  const lastVersionRef = useRef(0)

  const loadBrands = useCallback(async () => {
    try {
      const list = await listBrands()
      setBrands(list)
      setBrandsError(null)
      setSelectedBrandId((previous) => {
        if (list.some((brand) => brand.id === previous)) {
          return previous
        }
        return list.find((brand) => brand.is_default)?.id ?? list[0]?.id ?? ''
      })
    } catch (reason: unknown) {
      setBrandsError(errorMessage(reason))
    } finally {
      setBrandsLoading(false)
    }
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => void loadBrands(), 0)
    return () => window.clearTimeout(timer)
  }, [loadBrands])

  useEffect(() => {
    let cancelled = false
    listPlatforms()
      .then((list) => {
        if (cancelled) return
        setPlatforms(list)
        setPlatformsError(null)
        setSelectedPlatformId((previous) => {
          if (list.some((item) => item.id === previous)) return previous
          return list[0]?.id ?? ''
        })
        setSelectedPlacementId((previous) =>
          list[0]?.placements.some((item) => item.id === previous)
            ? previous
            : list[0]?.default_placement_id ?? '',
        )
      })
      .catch((reason: unknown) => {
        if (!cancelled) setPlatformsError(errorMessage(reason))
      })
      .finally(() => {
        if (!cancelled) setPlatformsLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const loadAssets = useCallback(async (showLoading = true) => {
    if (showLoading) {
      setAssetsLoading(true)
    }
    try {
      const list = await listAssets()
      setAssets(list)
      setProducts((previous) =>
        previous.map((item) => list.find((asset) => asset.id === item.id) ?? item),
      )
      setAssetsError(null)
    } catch (reason: unknown) {
      setAssetsError(errorMessage(reason))
    } finally {
      setAssetsLoading(false)
    }
  }, [])

  const loadVersions = useCallback(async (taskId: string) => {
    setVersionsLoading(true)
    try {
      setVersions(await listVersions(taskId))
    } catch (reason: unknown) {
      setError(errorMessage(reason))
    } finally {
      setVersionsLoading(false)
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    listAssets()
      .then((list) => {
        if (!cancelled) {
          setAssets(list)
          setAssetsError(null)
        }
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setAssetsError(errorMessage(reason))
        }
      })
      .finally(() => {
        if (!cancelled) {
          setAssetsLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    const taskId = readTaskId()
    if (!taskId) {
      return
    }
    getTask(taskId)
      .then((restored) => {
        setTask(restored)
        lastVersionRef.current = restored.current_version
        const ids =
          restored.input.product_asset_ids ??
          (restored.input.product_asset_id
            ? [restored.input.product_asset_id]
            : [])
        setProducts(
          ids
            .map((id) => assetFromTask(restored, 'product', id))
            .filter((asset): asset is Asset => Boolean(asset)),
        )
        const restoredBrandId = restored.input.brand_id ?? restored.brand?.id ?? ''
        if (restoredBrandId) {
          setSelectedBrandId(restoredBrandId)
        }
        const restoredMode =
          restored.input.brand_mode ??
          (restoredBrandId
            ? 'preset'
            : (restored.input.style_reference_asset_ids?.length ?? 0) > 0
              ? 'reference'
              : 'none')
        setBrandMode(restoredMode)
        setStyleReferences(
          (restored.input.style_reference_asset_ids ?? [])
            .map((id) => assetFromTask(restored, 'brand_reference', id))
            .filter((asset): asset is Asset => Boolean(asset)),
        )
        if (restored.platform?.id) {
          setSelectedPlatformId(restored.platform.id)
          setSelectedPlacementId(restored.placement?.id ?? restored.platform.default_placement_id)
        }
        if (restored.input.merchant_info) {
          setMerchantInfo(restored.input.merchant_info)
        }
        setPrompt(restored.input.prompt ?? '')
        setCandidateCount(restored.input.candidate_count ?? 1)
        if (restored.status === 'succeeded') {
          void loadVersions(restored.id)
        }
        void listAssets()
          .then((list) => {
            setProducts((previous) =>
              previous.map((item) => list.find((asset) => asset.id === item.id) ?? item),
            )
          })
          .catch(() => undefined)
      })
      .catch((reason: unknown) => {
        setError(errorMessage(reason))
        updateUrl({ taskId: null })
      })
      .finally(() => setBusy(false))
  }, [loadVersions])

  const candidateBatchId = task?.input.candidate_batch_id ?? null
  useEffect(() => {
    if (!candidateBatchId) {
      return
    }
    let cancelled = false
    const refresh = async () => {
      try {
        const list = await listTaskBatch(candidateBatchId)
        if (!cancelled) setCandidateTasks(list)
      } catch {
        // keep the current candidate list while the batch is still being created
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
        const next = await getTask(taskId)
        setTask(next)
        if (
          next.status === 'succeeded' &&
          next.current_version !== lastVersionRef.current
        ) {
          lastVersionRef.current = next.current_version
          void loadVersions(next.id)
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

  const active = Boolean(task && ['queued', 'running'].includes(task.status))
  const formBusy = busy || active
  const activeSelectedLayerId =
    selectedLayerId &&
    task?.design?.layers?.some((layer) => layer.id === selectedLayerId)
      ? selectedLayerId
      : null
  const selectedBrand = brands.find((brand) => brand.id === selectedBrandId) ?? null

  const handleBrandChange = (brandId: string) => {
    setSelectedBrandId(brandId)
    setBrandMode('preset')
  }

  const handleBrandModeChange = (mode: BrandMode) => {
    setBrandMode(mode)
    if (mode === 'preset' && !selectedBrandId) {
      setSelectedBrandId(
        brands.find((brand) => brand.is_default)?.id ?? brands[0]?.id ?? '',
      )
    }
  }

  const handleUploadStyleReferences = async (files: File[]) => {
    const remaining = Math.max(0, 3 - styleReferences.length)
    const accepted = files.slice(0, remaining)
    if (!accepted.length) return
    setStyleReferencesUploading(true)
    setError(null)
    try {
      const uploaded = await Promise.all(accepted.map((file) => uploadAsset(file, 'brand_reference')))
      setStyleReferences((previous) => [...previous, ...uploaded].slice(0, 3))
    } catch (reason: unknown) {
      setError(errorMessage(reason))
    } finally {
      setStyleReferencesUploading(false)
    }
  }

  const handleViewChange = (next: AppView) => {
    setPickerKind(null)
    setView(next)
    updateUrl({ view: next })
    if (next === 'assets') {
      void loadAssets()
    }
    if (next === 'brands') {
      void loadBrands()
    }
  }

  const handlePickProducts = () => {
    setPickerKind('product')
    if (assets.length === 0) {
      void loadAssets()
    }
  }

  const handleSelectProducts = (selected: Asset[]) => {
    setProducts(selected)
    setPickerKind(null)
  }

  const handleRemoveProduct = (assetId: string) => {
    setProducts((previous) => previous.filter((asset) => asset.id !== assetId))
  }

  const handleUseForPoster = (asset: Asset) => {
    setProducts((previous) =>
      previous.some((item) => item.id === asset.id) || previous.length >= 4
        ? previous
        : [...previous, asset],
    )
    setPickerKind(null)
    handleViewChange('poster')
  }

  const handleUpload = async (kind: AssetKind, file: File): Promise<boolean> => {
    setUploadingKind(kind)
    setAssetsError(null)
    try {
      await uploadAsset(file, kind)
      await loadAssets()
      return true
    } catch (reason: unknown) {
      setAssetsError(errorMessage(reason))
      return false
    } finally {
      setUploadingKind(null)
    }
  }

  const handleUseSamples = async (): Promise<string | null> => {
    setAssetsError(null)
    try {
      const result = await getSamples()
      await loadAssets()
      if (result.skipped.length > 0) {
        return `已载入 ${result.imported.length} 个素材，跳过 ${result.skipped.length} 个（${result.skipped
          .map((item) => item.file)
          .join('、')}）`
      }
      return `已载入 ${result.imported.length} 个素材`
    } catch (reason: unknown) {
      setAssetsError(errorMessage(reason))
      return null
    }
  }

  const handleDelete = async (asset: Asset): Promise<boolean> => {
    try {
      await deleteAsset(asset.id, true)
      setAssets((previous) => previous.filter((item) => item.id !== asset.id))
      setProducts((previous) => previous.filter((item) => item.id !== asset.id))
      return true
    } catch (reason: unknown) {
      setAssetsError(errorMessage(reason))
      return false
    }
  }

  const handleGenerate = async () => {
    if (products.length === 0) {
      setError('请先从资产管理选择参考素材')
      handleViewChange('assets')
      return
    }
    if (brandMode === 'preset' && !brands.some((brand) => brand.id === selectedBrandId)) {
      setError('请选择一个预设品牌风格')
      return
    }
    const platformForTask =
      platforms.find((item) => item.id === selectedPlatformId) ?? platforms[0]
    if (!platformForTask) {
      setError('请先选择发布平台')
      return
    }
    const placementForTask =
      platformForTask.placements.find((item) => item.id === selectedPlacementId) ??
      platformForTask.placements.find((item) => item.id === platformForTask.default_placement_id)
    if (!placementForTask) {
      setError('请选择发布用途')
      return
    }
    if (
      platformForTask.contact_policy === 'required' &&
      (!merchantInfo.address.trim() || !merchantInfo.phone.trim())
    ) {
      setError('该平台必须填写商家地址和联系电话')
      return
    }
    setBusy(true)
    setError(null)
    setTask(null)
    setSelectedLayerId(null)
    setVersions([])
    lastVersionRef.current = 0
    updateUrl({ taskId: null })
    try {
      const batchId = crypto.randomUUID()
      const created = await Promise.all(
        Array.from({ length: candidateCount }, (_, index) =>
          createTask({
            product_asset_ids: products.map((asset) => asset.id),
            brand_id: brandMode === 'preset' ? selectedBrandId : null,
            brand_mode: brandMode,
            style_reference_asset_ids:
              brandMode === 'reference' ? styleReferences.map((asset) => asset.id) : [],
            prompt,
            generation_mode: 'reference_guided',
            platform_id: platformForTask.id,
            placement_id: placementForTask.id,
            merchant_info:
              platformForTask.contact_policy === 'required' ? merchantInfo : null,
            candidate_count: candidateCount,
            candidate_batch_id: batchId,
            candidate_index: index,
          }),
        ),
      )
      setCandidateTasks(created)
      setTask(created[0])
      updateUrl({ taskId: created[0].id })
    } catch (reason: unknown) {
      setError(errorMessage(reason))
    } finally {
      setBusy(false)
    }
  }

  const handleResume = async () => {
    if (!task) {
      return
    }
    setBusy(true)
    setError(null)
    try {
      setTask(await resumeTask(task.id))
    } catch (reason: unknown) {
      setError(errorMessage(reason))
    } finally {
      setBusy(false)
    }
  }

  const handleRegenerateComponent = async (layerId: string) => {
    if (!task) {
      return
    }
    setError(null)
    try {
      setTask(await regenerateComponent(task.id, layerId))
    } catch (reason: unknown) {
      setError(errorMessage(reason))
    }
  }

  const handleSendEdit = async (instruction: string) => {
    if (!task) {
      return
    }
    setError(null)
    try {
      setTask(await createEdit(task.id, instruction))
      setStudioTab('chat')
    } catch (reason: unknown) {
      setError(errorMessage(reason))
    }
  }

  const handlePatchLayer = async (layerId: string, patch: LayerPatch) => {
    if (!task) {
      return
    }
    setError(null)
    try {
      setTask(await patchLayer(task.id, layerId, patch))
    } catch (reason: unknown) {
      setError(errorMessage(reason))
    }
  }

  const handleRestore = async (version: number) => {
    if (!task) {
      return
    }
    setError(null)
    try {
      const next = await restoreVersion(task.id, version)
      setTask(next)
      lastVersionRef.current = next.current_version
      await loadVersions(next.id)
    } catch (reason: unknown) {
      setError(errorMessage(reason))
    }
  }

  const selectedPlatform =
    platforms.find((item) => item.id === selectedPlatformId) ?? platforms[0] ?? null
  const selectedPlacement =
    selectedPlatform?.placements.find((item) => item.id === selectedPlacementId) ??
    selectedPlatform?.placements.find((item) => item.id === selectedPlatform.default_placement_id) ??
    null
  const activePlatform = task?.platform ?? selectedPlatform
  const activePlacement = task?.placement ?? selectedPlacement
  const displayRatio = activePlacement?.ratio ?? task?.input.ratio ?? '9:16'

  const handleSelectCandidate = (candidate: Task) => {
    setTask(candidate)
    setSelectedLayerId(null)
    setVersions([])
    lastVersionRef.current = candidate.current_version
    updateUrl({ taskId: candidate.id })
  }

  const studioReady = Boolean(
    task?.status === 'succeeded' && task.design?.layers?.length,
  )
  const studioBusy = Boolean(task?.edit.status === 'running')

  return (
    <div className="app-layout">
      <Sidebar view={view} onChange={handleViewChange} />
      <div className="app-main">
        <header className="topbar">
          <div className="topbar-title">
            <span className="topbar-kicker">
              {view === 'poster'
              ? 'POSTER STUDIO'
              : view === 'whole'
                ? 'WHOLE IMAGE STUDIO'
                : view === 'brands'
                  ? 'BRAND LIBRARY'
                  : 'ASSET LIBRARY'}
            </span>
            <strong>
              {view === 'poster'
                ? '海报生成'
                : view === 'whole'
                  ? '自由设计'
                  : view === 'brands'
                    ? '品牌风格'
                    : '资产管理'}
            </strong>
          </div>
          <div className="topbar-meta">
            <span className="status-dot status-dot-live" aria-hidden="true" />
            <span>
              {view === 'whole'
                ? 'core-ai · 整图生成与编辑'
                : view === 'brands'
                  ? 'MongoDB · 品牌配置与参考图'
                  : 'core-ai · 分层编辑'}
            </span>
            {view === 'poster' ? (
              <span className="meta-chip">
                {activePlatform?.name ?? (task ? '历史任务' : '发布平台')} · {displayRatio}
              </span>
            ) : null}
            {view === 'poster' ? (
              <span className="meta-chip">
                {brandMode === 'preset'
                  ? selectedBrand?.name ?? '预设风格'
                  : brandMode === 'reference'
                    ? '参考图'
                    : '无风格'}
              </span>
            ) : null}
          </div>
        </header>

        {error ? (
          <div className="global-error" role="alert">
            <span>{error}</span>
            <button type="button" onClick={() => setError(null)} aria-label="关闭提示">
              关闭
            </button>
          </div>
        ) : null}

        {view === 'poster' ? (
          <main className={`workspace${studioReady ? ' has-studio' : ''}`}>
            <GenerateForm
              brands={brands}
              selectedBrandId={selectedBrandId}
              brandsLoading={brandsLoading}
              brandsError={brandsError}
              brandMode={brandMode}
              styleReferences={styleReferences}
              styleReferencesUploading={styleReferencesUploading}
              platforms={platforms}
              platformsLoading={platformsLoading}
              platformsError={platformsError}
              selectedPlatformId={selectedPlatform?.id ?? ''}
              selectedPlacementId={selectedPlacement?.id ?? ''}
              merchantInfo={merchantInfo}
              products={products}
              prompt={prompt}
              candidateCount={candidateCount}
              busy={formBusy || studioBusy}
              onBrandChange={handleBrandChange}
              onBrandModeChange={handleBrandModeChange}
              onUploadStyleReferences={(files) => void handleUploadStyleReferences(files)}
              onRemoveStyleReference={(assetId) =>
                setStyleReferences((previous) => previous.filter((asset) => asset.id !== assetId))
              }
              onPlatformChange={(platformId) => {
                const next = platforms.find((item) => item.id === platformId)
                setSelectedPlatformId(platformId)
                setSelectedPlacementId(next?.default_placement_id ?? '')
              }}
              onPlacementChange={setSelectedPlacementId}
              onMerchantChange={setMerchantInfo}
              onPickProducts={handlePickProducts}
              onRemoveProduct={handleRemoveProduct}
              onPromptChange={setPrompt}
              onCandidateCountChange={setCandidateCount}
              onGoAssets={() => handleViewChange('assets')}
              onGenerate={handleGenerate}
            />
            <ResultPanel
              task={task}
              busy={busy}
              selectedLayerId={activeSelectedLayerId}
              onSelectLayer={setSelectedLayerId}
              onPatchLayer={handlePatchLayer}
              onResume={handleResume}
              onRetry={handleGenerate}
              candidates={visibleCandidates}
              onSelectCandidate={handleSelectCandidate}
            />
            {studioReady && task?.design ? (
              <aside className="studio-inspector" aria-label="编辑工作台">
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
                        <span className="tab-count tnum">{versions.length || task.current_version}</span>
                      ) : null}
                      {tab.key === 'layers' ? (
                        <span className="tab-count tnum">
                          {task?.design?.layers?.length ?? 0}
                        </span>
                      ) : null}
                    </button>
                  ))}
                </div>
                <div className="studio-tab-body">
                  {studioTab === 'chat' ? (
                    <ChatPanel
                      messages={task.messages}
                      edit={task.edit}
                      stage={task.stage}
                      disabled={!studioReady}
                      onSend={handleSendEdit}
                    />
                  ) : null}
                  {studioTab === 'layers' ? (
                    <LayerPanel
                      design={task.design}
                      selectedId={activeSelectedLayerId}
                      busy={studioBusy}
                      onSelect={setSelectedLayerId}
                      onPatch={handlePatchLayer}
                      onRegenerateComponent={handleRegenerateComponent}
                    />
                  ) : null}
                  {studioTab === 'versions' ? (
                    <VersionPanel
                      versions={versions}
                      currentVersion={task.current_version}
                      busy={versionsLoading || studioBusy}
                      onRestore={handleRestore}
                    />
                  ) : null}
                </div>
              </aside>
            ) : null}
          </main>
        ) : view === 'whole' ? (
          <WholeImageStudio
            brands={brands}
            brandsLoading={brandsLoading}
            brandsError={brandsError}
            platforms={platforms}
            platformsLoading={platformsLoading}
            platformsError={platformsError}
            assets={assets}
            assetsLoading={assetsLoading}
            onLoadAssets={() => void loadAssets()}
            onGoAssets={() => handleViewChange('assets')}
          />
        ) : view === 'brands' ? (
          <BrandStudio
            brands={brands}
            brandsLoading={brandsLoading}
            brandsError={brandsError}
            onRefreshBrands={loadBrands}
          />
        ) : (
          <AssetLibrary
            assets={assets}
            loading={assetsLoading}
            error={assetsError}
            uploadingKind={uploadingKind}
            selectedProductIds={products.map((asset) => asset.id)}
            onUpload={handleUpload}
            onUseSamples={handleUseSamples}
            onUseForPoster={handleUseForPoster}
            onDelete={handleDelete}
            onRefresh={() => void loadAssets()}
          />
        )}

        <footer className="app-footer">
          {view === 'poster' ? (
            <>
              <span>文字与图片分层编辑 · 底图独立版本</span>
              <span className="footer-sep" aria-hidden="true" />
              <span>刷新页面不会丢失当前任务</span>
            </>
          ) : view === 'whole' ? (
            <>
              <span>模型直出完整图片 · 不做图层拆分</span>
              <span className="footer-sep" aria-hidden="true" />
              <span>每次修改都会保存为独立版本</span>
            </>
          ) : view === 'brands' ? (
            <>
              <span>品牌配置保存在 MongoDB · 新任务立即生效</span>
              <span className="footer-sep" aria-hidden="true" />
              <span>旧任务继续使用创建时的品牌快照</span>
            </>
          ) : (
            <span>素材保存在腾讯云 COS · 生成海报时从资产库选择</span>
          )}
        </footer>
      </div>

      <AssetPicker
        key={pickerKind ?? 'closed'}
        open={pickerKind !== null}
        kind={pickerKind ?? 'product'}
        assets={assets}
        loading={assetsLoading}
        multi
        selectedIds={products.map((asset) => asset.id)}
        max={4}
        onClose={() => setPickerKind(null)}
        onConfirm={handleSelectProducts}
        onGoAssets={() => handleViewChange('assets')}
      />
    </div>
  )
}
