import { useCallback, useEffect, useState } from 'react'
import AssetLibrary from './components/AssetLibrary'
import BrandStudio from './components/BrandStudio'
import PosterEditStudio from './components/PosterEditStudio'
import Sidebar, { type AppView } from './components/Sidebar'
import TaskCenter from './components/TaskCenter'
import WholeImageStudio from './components/WholeImageStudio'
import {
  ApiError,
  deleteAsset,
  listAssets,
  listBrands,
  listPlatforms,
  uploadAsset,
} from './api'
import type { Asset, BrandStyle, PlatformPreset } from './types'

function readView(): AppView {
  const value = new URLSearchParams(window.location.search).get('view')
  if (value === 'assets') {
    return 'assets'
  }
  if (value === 'brands') {
    return 'brands'
  }
  if (value === 'poster_edit') {
    return 'poster_edit'
  }
  if (value === 'tasks') {
    return 'tasks'
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
  poster_edit: {
    kicker: 'POSTER EDIT',
    title: '修改海报',
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
  const [uploading, setUploading] = useState(false)
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

  const loadAssets = useCallback(async (showLoading = true) => {
    if (showLoading) {
      setAssetsLoading(true)
    }
    try {
      setAssets(await listAssets())
      setAssetsError(null)
    } catch (reason: unknown) {
      setAssetsError(errorMessage(reason))
    } finally {
      setAssetsLoading(false)
    }
  }, [])

  useEffect(() => {
    const url = new URL(window.location.href)
    if (url.searchParams.get('view') === 'poster') {
      url.searchParams.delete('view')
      url.searchParams.delete('task')
      window.history.replaceState({}, '', url)
    } else if (url.searchParams.has('task')) {
      url.searchParams.delete('task')
      window.history.replaceState({}, '', url)
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    listBrands()
      .then((list) => {
        if (!cancelled) {
          setBrands(list)
          setBrandsError(null)
        }
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
    listPlatforms()
      .then((list) => {
        if (!cancelled) {
          setPlatforms(list)
          setPlatformsError(null)
        }
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setPlatformsError(errorMessage(reason))
        }
      })
      .finally(() => {
        if (!cancelled) {
          setPlatformsLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  const handleViewChange = (next: AppView) => {
    if (next === view && (next === 'whole' || next === 'poster_edit')) {
      setStudioRevision((current) => current + 1)
    }
    setView(next)
    updateUrl(next)
    if (next === 'assets') {
      void loadAssets()
    }
    if (next === 'brands') {
      void loadBrands()
    }
  }

  const handleUpload = async (file: File): Promise<boolean> => {
    setUploading(true)
    setAssetsError(null)
    try {
      await uploadAsset(file)
      await loadAssets()
      return true
    } catch (reason: unknown) {
      setAssetsError(errorMessage(reason))
      return false
    } finally {
      setUploading(false)
    }
  }

  const handleDelete = async (asset: Asset): Promise<boolean> => {
    try {
      await deleteAsset(asset.id, true)
      setAssets((previous) => previous.filter((item) => item.id !== asset.id))
      return true
    } catch (reason: unknown) {
      setAssetsError(errorMessage(reason))
      return false
    }
  }

  const handleOpenTask = (taskId: string) => {
    const url = new URL(window.location.href)
    url.searchParams.delete('view')
    url.searchParams.set('whole', taskId)
    window.history.replaceState({}, '', url)
    setView('whole')
  }

  const handleOpenPosterBatch = (batchId: string) => {
    const url = new URL(window.location.href)
    url.searchParams.set('view', 'poster_edit')
    url.searchParams.set('poster_batch', batchId)
    url.searchParams.delete('poster')
    url.searchParams.delete('whole')
    window.history.replaceState({}, '', url)
    setView('poster_edit')
  }

  const handleCreateTask = (kind: 'generate' | 'edit') => {
    const next: AppView = kind === 'edit' ? 'poster_edit' : 'whole'
    updateUrl(next)
    setStudioRevision((current) => current + 1)
    setView(next)
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
          {viewMeta.meta ? (
            <div className="topbar-meta">
              <span className="status-dot status-dot-live" aria-hidden="true" />
              <span>{viewMeta.meta}</span>
            </div>
          ) : null}
        </header>

        {view === 'tasks' ? (
          <TaskCenter
            onOpenTask={handleOpenTask}
            onOpenPosterBatch={handleOpenPosterBatch}
            onCreate={handleCreateTask}
          />
        ) : view === 'whole' ? (
          <WholeImageStudio
            key={`whole-${studioRevision}`}
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
        ) : view === 'poster_edit' ? (
          <PosterEditStudio
            key={`poster-edit-${studioRevision}`}
            assets={assets}
            assetsLoading={assetsLoading}
            onLoadAssets={() => void loadAssets()}
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
            uploading={uploading}
            onUpload={handleUpload}
            onDelete={handleDelete}
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
          ) : view === 'brands' || view === 'poster_edit' ? null : (
            <span>素材保存在项目内本地目录 · 海报设计时从资产库选择</span>
          )}
        </footer>
      </div>
    </div>
  )
}
