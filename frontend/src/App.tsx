import { useCallback, useEffect, useRef, useState } from 'react'
import AssetLibrary from './components/AssetLibrary'
import AssetPicker from './components/AssetPicker'
import ChatPanel from './components/ChatPanel'
import GenerateForm from './components/GenerateForm'
import LayerPanel from './components/LayerPanel'
import ResultPanel from './components/ResultPanel'
import Sidebar, { type AppView } from './components/Sidebar'
import VersionPanel from './components/VersionPanel'
import {
  ApiError,
  createEdit,
  createTask,
  deleteAsset,
  getSamples,
  getTask,
  listAssets,
  listBrands,
  listVersions,
  patchLayer,
  resumeTask,
  restoreVersion,
  uploadAsset,
} from './api'
import type {
  Asset,
  AssetKind,
  BrandStyle,
  LayerPatch,
  Ratio,
  Task,
  VersionSummary,
} from './types'

type StudioTab = 'chat' | 'layers' | 'versions'

function readTaskId(): string | null {
  return new URLSearchParams(window.location.search).get('task')
}

function readView(): AppView {
  return new URLSearchParams(window.location.search).get('view') === 'assets'
    ? 'assets'
    : 'poster'
}

function updateUrl(next: { view?: AppView; taskId?: string | null }) {
  const url = new URL(window.location.href)
  if (next.view !== undefined) {
    if (next.view === 'assets') {
      url.searchParams.set('view', 'assets')
    } else {
      url.searchParams.delete('view')
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

function inferRatio(canvas: { width: number; height: number }): Ratio {
  const value = canvas.width / canvas.height
  if (Math.abs(value - 9 / 16) < 0.02) {
    return '9:16'
  }
  if (Math.abs(value - 4 / 5) < 0.02) {
    return '4:5'
  }
  if (Math.abs(value - 16 / 9) < 0.02) {
    return '16:9'
  }
  return '1:1'
}

function assetFromTask(
  task: Task,
  kind: AssetKind,
  assetId?: string | null,
): Asset | null {
  const id =
    assetId ??
    (kind === 'product' ? task.input.product_asset_id : task.input.logo_asset_id)
  if (!id) {
    return null
  }
  return {
    id,
    kind,
    original_name: kind === 'product' ? '已保存的商品图' : '已保存的 Logo',
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
  const [logo, setLogo] = useState<Asset | null>(null)
  const [logoManual, setLogoManual] = useState(false)
  const [brands, setBrands] = useState<BrandStyle[]>([])
  const [brandsLoading, setBrandsLoading] = useState(true)
  const [brandsError, setBrandsError] = useState<string | null>(null)
  const [selectedBrandId, setSelectedBrandId] = useState('')
  const [ratio, setRatio] = useState<Ratio>('9:16')
  const [prompt, setPrompt] = useState('')
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

  useEffect(() => {
    let cancelled = false
    listBrands()
      .then((list) => {
        if (cancelled) {
          return
        }
        setBrands(list)
        setBrandsError(null)
        setSelectedBrandId((previous) => {
          if (previous) {
            return previous
          }
          return list.find((brand) => brand.is_default)?.id ?? list[0]?.id ?? ''
        })
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setBrandsError(errorMessage(reason))
        }
      })
      .finally(() => {
        if (!cancelled) {
          setBrandsLoading(false)
        }
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
      setLogo((previous) =>
        previous
          ? list.find((asset) => asset.id === previous.id) ?? previous
          : previous,
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
        setLogo(assetFromTask(restored, 'logo'))
        setLogoManual(true)
        const restoredBrandId = restored.input.brand_id ?? restored.brand?.id ?? ''
        if (restoredBrandId) {
          setSelectedBrandId(restoredBrandId)
        }
        setRatio(restored.input.ratio ?? inferRatio(restored.canvas))
        setPrompt(restored.input.prompt ?? '')
        if (restored.status === 'succeeded') {
          void loadVersions(restored.id)
        }
        void listAssets()
          .then((list) => {
            setProducts((previous) =>
              previous.map((item) => list.find((asset) => asset.id === item.id) ?? item),
            )
            setLogo((previous) =>
              previous
                ? list.find((asset) => asset.id === previous.id) ?? previous
                : previous,
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
  const autoLogo = (() => {
    if (!selectedBrand) {
      return null
    }
    const keywords = selectedBrand.logo_keywords.map((item) => item.toLowerCase())
    return (
      assets.find(
        (asset) =>
          asset.kind === 'logo' &&
          keywords.some((keyword) =>
            asset.original_name.toLowerCase().includes(keyword),
          ),
      ) ?? null
    )
  })()
  const effectiveLogo = logo ?? (logoManual ? null : autoLogo)

  const handleBrandChange = (brandId: string) => {
    if (brandId === selectedBrandId) {
      return
    }
    setSelectedBrandId(brandId)
    if (!logoManual) {
      setLogo(null)
    }
  }

  const handleViewChange = (next: AppView) => {
    setPickerKind(null)
    setView(next)
    updateUrl({ view: next })
    if (next === 'assets') {
      void loadAssets()
    }
  }

  const handlePickProducts = () => {
    setPickerKind('product')
    if (assets.length === 0) {
      void loadAssets()
    }
  }

  const handlePickLogo = () => {
    setPickerKind('logo')
    if (assets.length === 0) {
      void loadAssets()
    }
  }

  const handleSelectProducts = (selected: Asset[]) => {
    setProducts(selected)
    setPickerKind(null)
  }

  const handleSelectLogo = (asset: Asset) => {
    setLogo(asset)
    setLogoManual(true)
    setPickerKind(null)
  }

  const handleRemoveProduct = (assetId: string) => {
    setProducts((previous) => previous.filter((asset) => asset.id !== assetId))
  }

  const handleUseForPoster = (asset: Asset) => {
    if (asset.kind === 'product') {
      setProducts((previous) =>
        previous.some((item) => item.id === asset.id) || previous.length >= 4
          ? previous
          : [...previous, asset],
      )
    } else {
      setLogo(asset)
      setLogoManual(true)
    }
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
      await deleteAsset(asset.id)
      setAssets((previous) => previous.filter((item) => item.id !== asset.id))
      setProducts((previous) => previous.filter((item) => item.id !== asset.id))
      if (effectiveLogo?.id === asset.id) {
        setLogo(null)
        setLogoManual(false)
      }
      setLogo((previous) => (previous?.id === asset.id ? null : previous))
      return true
    } catch (reason: unknown) {
      setAssetsError(errorMessage(reason))
      return false
    }
  }

  const handleGenerate = async () => {
    if (products.length === 0) {
      setError('请先从资产管理选择商品图')
      handleViewChange('assets')
      return
    }
    if (!selectedBrandId) {
      setError('请先选择品牌风格')
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
      const created = await createTask({
        product_asset_ids: products.map((asset) => asset.id),
        logo_asset_id: effectiveLogo?.id ?? null,
        brand_id: selectedBrandId,
        prompt,
        ratio,
      })
      setTask(created)
      updateUrl({ taskId: created.id })
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
              {view === 'poster' ? 'POSTER STUDIO' : 'ASSET LIBRARY'}
            </span>
            <strong>{view === 'poster' ? '海报生成' : '资产管理'}</strong>
          </div>
          <div className="topbar-meta">
            <span className="status-dot status-dot-live" aria-hidden="true" />
            <span>ToAPIs · 分层编辑</span>
            <span className="meta-chip">
              {ratio} · {ratio === '1:1' ? '1K' : '2K'}
            </span>
            {view === 'poster' && selectedBrand ? (
              <span className="meta-chip">{selectedBrand.name}</span>
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
              products={products}
              logo={effectiveLogo}
              ratio={ratio}
              prompt={prompt}
              busy={formBusy || studioBusy}
              onBrandChange={handleBrandChange}
              onPickProducts={handlePickProducts}
              onRemoveProduct={handleRemoveProduct}
              onPickLogo={handlePickLogo}
              onClearLogo={() => {
                setLogo(null)
                setLogoManual(true)
              }}
              onRatioChange={setRatio}
              onPromptChange={setPrompt}
              onGoAssets={() => handleViewChange('assets')}
              onGenerate={handleGenerate}
            />
            <ResultPanel
              task={task}
              busy={busy}
              selectedLayerId={activeSelectedLayerId}
              onSelectLayer={setSelectedLayerId}
              onResume={handleResume}
              onRetry={handleGenerate}
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
        ) : (
          <AssetLibrary
            assets={assets}
            loading={assetsLoading}
            error={assetsError}
            uploadingKind={uploadingKind}
            selectedProductIds={products.map((asset) => asset.id)}
            selectedLogoId={effectiveLogo?.id ?? null}
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
              <span>文字与 Logo 分层编辑 · 底图独立版本</span>
              <span className="footer-sep" aria-hidden="true" />
              <span>刷新页面不会丢失当前任务</span>
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
        multi={pickerKind === 'product'}
        selectedIds={
          pickerKind === 'product'
            ? products.map((asset) => asset.id)
            : effectiveLogo
              ? [effectiveLogo.id]
              : []
        }
        max={4}
        onClose={() => setPickerKind(null)}
        onSelect={handleSelectLogo}
        onConfirm={handleSelectProducts}
        onGoAssets={() => handleViewChange('assets')}
      />
    </div>
  )
}
