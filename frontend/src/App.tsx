import { useCallback, useEffect, useState } from 'react'
import AssetLibrary from './components/AssetLibrary'
import BrandStudio from './components/BrandStudio'
import Sidebar, { type AppView } from './components/Sidebar'
import TaskCenter from './components/TaskCenter'
import WholeImageStudio from './components/WholeImageStudio'
import AgentTestPage from './components/AgentTestPage'
import MerchantSwitcher from './components/MerchantSwitcher'
import PosterEditStudio from './components/PosterEditStudio'
import {
  ApiError,
  getMerchantContext,
  type MerchantContext,
  listAssetPage,
  listBrands,
  listPlatforms,
  merchantIdFromLocation,
  uploadAsset,
} from './api'
import type { Asset, BrandStyle, MerchantProfile, PlatformPreset } from './types'

function readView(): AppView {
  const value = new URLSearchParams(window.location.search).get('view')
  if (value === 'agent-tests') return 'agent-tests'
  if (value === 'assets') {
    return 'assets'
  }
  if (value === 'brands') {
    return 'brands'
  }
  if (value === 'tasks') {
    return 'tasks'
  }
  if (value === 'poster-edit') {
    return 'poster-edit'
  }
  return 'whole'
}

function updateUrl(next: AppView) {
  const url = new URL(window.location.href)
  if (next === 'whole') {
    url.searchParams.delete('view')
  } else {
    url.searchParams.set('view', next)
  }
  url.searchParams.delete('task')
  url.searchParams.delete('whole')
  url.searchParams.delete('poster_batch')
  url.searchParams.delete('poster')
  url.searchParams.delete('candidate')
  url.searchParams.delete('replacement')
  url.searchParams.delete('tab')
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

const VIEW_META: Record<
  AppView,
  { kicker: string; title: string; meta: string | null }
> = {
  'agent-tests': {
    kicker: 'AGENT BENCHMARK',
    title: 'Agent 测试',
    meta: 'image-editor · 固定五例',
  },
  'poster-edit': {
    kicker: 'POSTER EDIT',
    title: '海报修改',
    meta: '参考图 +文字 · image-editor',
  },
  whole: {
    kicker: 'POSTER STUDIO',
    title: '海报设计',
    meta: 'core-ai · 整图生成与编辑',
  },
  tasks: {
    kicker: 'TASK CENTER',
    title: '任务中心',
    meta: null,
  },
  assets: {
    kicker: 'ASSET LIBRARY',
    title: '资产管理',
    meta: null,
  },
  brands: {
    kicker: 'BRAND LIBRARY',
    title: '品牌风格',
    meta: null,
  },
}

export default function App() {
  const [initialMerchantId] = useState(() => merchantIdFromLocation())
  const [view, setView] = useState<AppView>(() => readView())
  const [brands, setBrands] = useState<BrandStyle[]>([])
  const [brandsLoading, setBrandsLoading] = useState(true)
  const [brandsError, setBrandsError] = useState<string | null>(null)
  const [platforms, setPlatforms] = useState<PlatformPreset[]>([])
  const [platformsLoading, setPlatformsLoading] = useState(true)
  const [platformsError, setPlatformsError] = useState<string | null>(null)
  const [assets, setAssets] = useState<Asset[]>([])
  const [assetsLoading, setAssetsLoading] = useState(true)
  const [assetsError, setAssetsError] = useState<string | null>(null)
  const [assetTotal, setAssetTotal] = useState(0)
  const [assetQuery, setAssetQuery] = useState('')
  const [loadingMoreAssets, setLoadingMoreAssets] = useState(false)
  const [uploadingAsset, setUploadingAsset] = useState(false)
  const [context, setContext] = useState<MerchantContext | null>(null)
  const [contextLoading, setContextLoading] = useState(Boolean(initialMerchantId))
  const [contextError, setContextError] = useState<string | null>(
    initialMerchantId ? null : '缺少 merchant_id，请从 AM Console 重新选择商家进入',
  )
  const [studioRevision, setStudioRevision] = useState(0)

  const loadBrands = useCallback(async (showLoading = true) => {
    if (showLoading) {
      setBrandsLoading(true)
    }
    try {
      setBrands(await listBrands())
      setBrandsError(null)
    } catch (reason: unknown) {
      setBrandsError(errorMessage(reason))
    } finally {
      setBrandsLoading(false)
    }
  }, [])

  const loadAssets = useCallback(async (
    keyword = '',
    append = false,
    skip = 0,
    showLoading = true,
  ) => {
    if (append) {
      setLoadingMoreAssets(true)
    } else if (showLoading) {
      setAssetsLoading(true)
    }
    try {
      const page = await listAssetPage(keyword, skip, 24)
      setAssets((previous) => append ? [...previous, ...page.items] : page.items)
      setAssetTotal(page.total)
      setAssetsError(null)
    } catch (reason: unknown) {
      setAssetsError(errorMessage(reason))
    } finally {
      setAssetsLoading(false)
      setLoadingMoreAssets(false)
    }
  }, [])

  useEffect(() => {
    const url = new URL(window.location.href)
    const requestedView = url.searchParams.get('view')
    if (requestedView && requestedView !== 'whole' && readView() === 'whole') {
      url.searchParams.delete('view')
      url.searchParams.delete('task')
      url.searchParams.delete('poster_batch')
      url.searchParams.delete('poster')
      url.searchParams.delete('candidate')
      url.searchParams.delete('replacement')
      url.searchParams.delete('tab')
      window.history.replaceState({}, '', url)
    } else if (url.searchParams.has('task')) {
      url.searchParams.delete('task')
      window.history.replaceState({}, '', url)
    }
  }, [])

  useEffect(() => {
    const merchantId = initialMerchantId
    if (!merchantId) return
    getMerchantContext()
      .then((value) => {
        setContext(value)
        setContextError(null)
        void loadBrands()
        listPlatforms()
          .then((list) => {
            setPlatforms(list)
            setPlatformsError(null)
          })
          .catch((reason: unknown) => setPlatformsError(errorMessage(reason)))
          .finally(() => setPlatformsLoading(false))
        void loadAssets('', false, 0, true)
      })
      .catch((reason: unknown) => setContextError(errorMessage(reason)))
      .finally(() => setContextLoading(false))
  }, [initialMerchantId, loadAssets, loadBrands])

  const handleViewChange = (next: AppView) => {
    if (next === view && next === 'whole') {
      setStudioRevision((current) => current + 1)
    }
    setView(next)
    updateUrl(next)
    if (next === 'assets') {
      void loadAssets(assetQuery, false, 0, true)
    }
    if (next === 'brands') {
      void loadBrands()
    }
  }

  const handleSearchAssets = (keyword: string) => {
    setAssetQuery(keyword)
    void loadAssets(keyword, false, 0, true)
  }

  const handleLoadMoreAssets = () => {
    void loadAssets(assetQuery, true, assets.length, false)
  }

  const handleDeleteAsset = (assetId: string) => {
    setAssets((previous) => previous.filter((item) => item.id !== assetId))
    setAssetTotal((current) => Math.max(0, current -1))
  }

  const handleUploadAsset = async (file: File): Promise<boolean> => {
    setUploadingAsset(true)
    setAssetsError(null)
    try {
      const uploaded = await uploadAsset(file)
      setAssets((previous) => [uploaded, ...previous.filter((item) => item.id !== uploaded.id)])
      setAssetTotal((current) => current + 1)
      return true
    } catch (reason: unknown) {
      setAssetsError(errorMessage(reason))
      return false
    } finally {
      setUploadingAsset(false)
    }
  }

  const handleOpenTask = (taskId: string) => {
    const url = new URL(window.location.href)
    url.searchParams.delete('view')
    url.searchParams.set('whole', taskId)
    window.history.replaceState({}, '', url)
    setView('whole')
  }

  const handleOpenReplacement = (taskId: string, wholeTaskId: string) => {
    updateUrl('whole')
    const url = new URL(window.location.href)
    url.searchParams.set('replacement', taskId)
    url.searchParams.set('whole', wholeTaskId)
    url.searchParams.set('tab', 'original_replace')
    window.history.replaceState({}, '', url)
    setStudioRevision((current) => current + 1)
    setView('whole')
  }

  const handleCreateTask = () => {
    updateUrl('whole')
    setStudioRevision((current) => current + 1)
    setView('whole')
  }

  if (contextLoading) {
    return <div className="app-loading">正在验证 AM 登录和商家权限…</div>
  }
  if (contextError || !context) {
    return (
      <div className="app-context-error">
        <strong>无法打开商家工作区</strong>
        <p>{contextError || '商家上下文不可用'}</p>
        <a className="mp-btn-primary" href="https://am.connexup-uat.net">返回 AM Console 重新进入</a>
      </div>
    )
  }

  const viewMeta = VIEW_META[view]

  return (
    <div className="app-layout">
      <Sidebar view={view} onChange={handleViewChange} />
      <div className="app-main">
        <header className="topbar">
          <div className="topbar-title">
            <span className="topbar-kicker">{viewMeta.kicker}</span>
            <strong>{viewMeta.title}</strong>
          </div>
          <MerchantSwitcher
            currentId={initialMerchantId}
            currentName={context?.merchant.merchant_name ?? viewMeta.meta ?? '商家工作区'}
          />
        </header>

        {view === 'poster-edit' ? (
            <PosterEditStudio
            assets={assets}
            assetsLoading={assetsLoading}
            onLoadAssets={() => void loadAssets()}
            onGoAssets={() => handleViewChange('assets')}
            />
 ) : view === 'agent-tests' ? (
          <AgentTestPage />
        ) : view === 'tasks' ? (
          <TaskCenter
            onOpenTask={handleOpenTask}
            onOpenReplacement={handleOpenReplacement}
            onCreate={handleCreateTask}
          />
        ) : view === 'whole' ? (
          <WholeImageStudio
            key={`whole-${studioRevision}`}
            merchantProfile={(context?.profile as MerchantProfile | null) ?? null}
    brands={brands}
            brandsLoading={brandsLoading}
            brandsError={brandsError}
            platforms={platforms}
            platformsLoading={platformsLoading}
            platformsError={platformsError}
            assets={assets}
            assetsLoading={assetsLoading}
            onLoadAssets={() => void loadAssets()}
            onRefreshBrands={loadBrands}
            onGoAssets={() => handleViewChange('assets')}
          />
        ) : view === 'brands' ? (
          <BrandStudio
            brands={brands}
            platforms={platforms}
            brandsLoading={brandsLoading}
            brandsError={brandsError}
            onRefreshBrands={loadBrands}
          />
        ) : (
          <AssetLibrary
            assets={assets}
            loading={assetsLoading}
            error={assetsError}
            total={assetTotal}
            query={assetQuery}
            onSearch={handleSearchAssets}
            onLoadMore={handleLoadMoreAssets}
            loadingMore={loadingMoreAssets}
            uploading={uploadingAsset}
            onUpload={handleUploadAsset}
            onDeleted={handleDeleteAsset}
          />
        )}

        <footer className="app-footer">
          {view === 'whole' ? (
            <>
              <span>接口与 Agent 双路改图 · 结果并排对比</span>
              <span className="footer-sep" aria-hidden="true" />
              <span>每轮结果需手动选择后才能继续修改</span>
            </>
          ) : view === 'tasks' ? (
            <span>任务按候选批次归档 · 点击封面回到工作台继续编辑</span>
          ) : view === 'agent-tests' ? (
            <span>原始输入 · 独立运行 · Trace 与耗时记录</span>
          ) : view === 'brands' ? null : (
            <span>资源库来自 AM Console 只读 · 临时上传保存在商家专属媒体目录</span>
          )}
        </footer>
      </div>
    </div>
  )
}
