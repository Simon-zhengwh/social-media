import { useCallback, useEffect, useRef, useState } from 'react'
import AssetPicker from './AssetPicker'
import WholeChatPanel from './WholeChatPanel'
import WholeImageForm from './WholeImageForm'
import WholeImageResultPanel from './WholeImageResultPanel'
import WholeVersionPanel from './WholeVersionPanel'
import {
  ApiError,
  createWholeImageEdit,
  createWholeImageTask,
  getWholeImageTask,
  listWholeImageTaskBatch,
  listWholeImageVersions,
  selectWholeImageVersion,
  uploadAsset,
} from '../api'
import type {
  Asset,
  AssetKind,
  BrandMode,
  BrandStyle,
  MerchantInfo,
  PlatformPreset,
  WholeImageTask,
  WholeImageVersion,
} from '../types'

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
  onGoAssets: () => void
}

type StudioTab = 'chat' | 'versions'

const TABS: Array<{ key: StudioTab; label: string }> = [
  { key: 'chat', label: '对话' },
  { key: 'versions', label: '版本' },
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
  kind: 'product' | 'logo' | 'brand_reference',
  assetId?: string | null,
): Asset | null {
  if (!assetId) {
    return null
  }
  return {
    id: assetId,
    kind,
    original_name: '已保存的参考素材',
    width: 0,
    height: 0,
    mime_type: 'image/png',
    url: `/api/assets/${assetId}/file`,
    created_at: task.created_at,
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
  onGoAssets,
}: Props) {
  const [task, setTask] = useState<WholeImageTask | null>(null)
  const [versions, setVersions] = useState<WholeImageVersion[]>([])
  const [products, setProducts] = useState<Asset[]>([])
  const [selectedPlatformId, setSelectedPlatformId] = useState('')
  const [selectedPlacementId, setSelectedPlacementId] = useState('')
  const [merchantInfo, setMerchantInfo] = useState<MerchantInfo>({ address: '', phone: '' })
  const [prompt, setPrompt] = useState('')
  const [candidateCount, setCandidateCount] = useState(1)
  const [candidateTasks, setCandidateTasks] = useState<WholeImageTask[]>([])
  const [selectedBrandId, setSelectedBrandId] = useState('')
  const [brandMode, setBrandMode] = useState<BrandMode>('preset')
  const [styleReferences, setStyleReferences] = useState<Asset[]>([])
  const [styleReferencesUploading, setStyleReferencesUploading] = useState(false)
  const [pickerKind, setPickerKind] = useState<AssetKind | null>(null)
  const [studioTab, setStudioTab] = useState<StudioTab>('chat')
  const [creating, setCreating] = useState(() => Boolean(readWholeTaskId()))
  const [versionsLoading, setVersionsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const pollRef = useRef<number | null>(null)
  const inFlight = useRef(false)
  const submitLock = useRef(false)
  const lastVersionRef = useRef<string | null>(null)

  const effectiveBrandId =
    selectedBrandId || brands.find((brand) => brand.is_default)?.id || brands[0]?.id || ''
  const selectedPlatform =
    platforms.find((item) => item.id === selectedPlatformId) ?? platforms[0] ?? null
  const selectedPlacement =
    selectedPlatform?.placements.find((item) => item.id === selectedPlacementId) ??
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
        setCandidateCount(restored.input.candidate_count ?? 1)
        if (restored.platform?.id) {
          setSelectedPlatformId(restored.platform.id)
          setSelectedPlacementId(restored.placement?.id ?? restored.platform.default_placement_id)
        }
        if (restored.input.merchant_info) {
          setMerchantInfo(restored.input.merchant_info)
        }
        setSelectedBrandId(restored.input.brand_id ?? '')
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
            .map((id) => assetFromTask(restored, 'brand_reference', id))
            .filter((asset): asset is Asset => Boolean(asset)),
        )
        const restoredProducts = (restored.input.product_asset_ids ?? [])
          .map((id) => assetFromTask(restored, 'product', id))
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
  const busy = creating || active || editRunning
  const canEdit = Boolean(task?.status === 'succeeded' && task.current_version_id)

  const handleGenerate = async () => {
    if (submitLock.current || busy) {
      return
    }
    if (!prompt.trim()) {
      setError('请先填写设计需求')
      return
    }
    if (products.length === 0) {
      setError('请先从资产管理选择参考素材')
      return
    }
    if (brandMode === 'preset' && !brands.some((brand) => brand.id === effectiveBrandId)) {
      setError('请选择一个预设品牌风格')
      return
    }
    if (!selectedPlatform || !selectedPlacement) {
      setError('请先选择发布平台和用途')
      return
    }
    if (
      selectedPlatform.contact_policy === 'required' &&
      (!merchantInfo.address.trim() || !merchantInfo.phone.trim())
    ) {
      setError('该平台必须填写商家地址和联系电话')
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
            prompt,
            brand_id: brandMode === 'preset' ? effectiveBrandId : null,
            brand_mode: brandMode,
            style_reference_asset_ids:
              brandMode === 'reference' ? styleReferences.map((asset) => asset.id) : [],
            product_asset_ids: products.map((asset) => asset.id),
            platform_id: selectedPlatform.id,
            placement_id: selectedPlacement.id,
            merchant_info:
              selectedPlatform.contact_policy === 'required' ? merchantInfo : null,
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
      await handleGenerate()
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
      const uploaded = await Promise.all(
        accepted.map((file) => uploadAsset(file, 'brand_reference')),
      )
      setStyleReferences((previous) => [...previous, ...uploaded].slice(0, 3))
    } catch (reason: unknown) {
      setError(errorMessage(reason))
    } finally {
      setStyleReferencesUploading(false)
    }
  }

  const openPicker = () => {
    setPickerKind('product')
    if (assets.length === 0) {
      onLoadAssets()
    }
  }

  return (
    <main className={`workspace${studioReady ? ' has-studio' : ''}`}>
      <WholeImageForm
        brands={brands}
        selectedBrandId={effectiveBrandId}
        brandsLoading={brandsLoading}
        brandsError={brandsError}
        brandMode={brandMode}
        styleReferences={styleReferences}
        styleReferencesUploading={styleReferencesUploading}
        platforms={platforms}
        platformsLoading={platformsLoading}
        platformsError={platformsError}
        selectedPlatformId={selectedPlatform?.id ?? selectedPlatformId}
        selectedPlacementId={selectedPlacement?.id ?? selectedPlacementId}
        merchantInfo={merchantInfo}
        products={products}
        prompt={prompt}
        candidateCount={candidateCount}
        busy={busy}
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
        onPickProducts={openPicker}
        onRemoveProduct={(id) =>
          setProducts((previous) => previous.filter((item) => item.id !== id))
        }
        onPromptChange={setPrompt}
        onCandidateCountChange={setCandidateCount}
        onGoAssets={onGoAssets}
        onGenerate={() => void handleGenerate()}
      />

      <WholeImageResultPanel
        task={task}
        busy={creating}
        onRetry={() => void handleRetry()}
        candidates={visibleCandidates}
        onSelectCandidate={handleSelectCandidate}
      />

      {studioReady ? (
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
        key={pickerKind ?? 'whole-closed'}
        open={pickerKind !== null}
        kind={pickerKind ?? 'product'}
        assets={assets}
        loading={assetsLoading}
        multi
        selectedIds={products.map((asset) => asset.id)}
        max={4}
        onClose={() => setPickerKind(null)}
        onConfirm={(selected) => {
          setProducts(selected)
          setPickerKind(null)
        }}
        onGoAssets={() => {
          setPickerKind(null)
          onGoAssets()
        }}
      />
    </main>
  )
}
