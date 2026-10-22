import { useCallback, useEffect, useState } from 'react'
import AssetLibrary from './components/AssetLibrary'
import BrandStudio from './components/BrandStudio'
import Sidebar, { type AppView } from './components/Sidebar'
import WholeImageStudio from './components/WholeImageStudio'
import {
  ApiError,
  deleteAsset,
  getSamples,
  listAssets,
  listBrands,
  listPlatforms,
  uploadAsset,
} from './api'
import type { Asset, AssetKind, BrandStyle, PlatformPreset } from './types'

function readView(): AppView {
  const value = new URLSearchParams(window.location.search).get('view')
  if (value === 'assets') {
    return 'assets'
  }
  if (value === 'brands') {
    return 'brands'
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
  const [uploadingKind, setUploadingKind] = useState<AssetKind | null>(null)

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
    setView(next)
    updateUrl(next)
    if (next === 'assets') {
      void loadAssets()
    }
    if (next === 'brands') {
      void loadBrands()
    }
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
      return true
    } catch (reason: unknown) {
      setAssetsError(errorMessage(reason))
      return false
    }
  }

  return (
    <div className="app-layout">
      <Sidebar view={view} onChange={handleViewChange} />
      <div className="app-main">
        <header className="topbar">
          <div className="topbar-title">
            <span className="topbar-kicker">
              {view === 'whole'
                ? 'WHOLE IMAGE STUDIO'
                : view === 'brands'
                  ? 'BRAND LIBRARY'
                  : 'ASSET LIBRARY'}
            </span>
            <strong>
              {view === 'whole' ? '自由设计' : view === 'brands' ? '品牌风格' : '资产管理'}
            </strong>
          </div>
          <div className="topbar-meta">
            <span className="status-dot status-dot-live" aria-hidden="true" />
            <span>
              {view === 'whole'
                ? 'core-ai · 整图生成与编辑'
                : view === 'brands'
                  ? 'MongoDB · 品牌配置与参考图'
                  : 'COS · 图片素材与参考图'}
            </span>
          </div>
        </header>

        {view === 'whole' ? (
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
            onUpload={handleUpload}
            onUseSamples={handleUseSamples}
            onDelete={handleDelete}
            onRefresh={() => void loadAssets()}
          />
        )}

        <footer className="app-footer">
          {view === 'whole' ? (
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
            <span>素材保存在腾讯云 COS · 自由设计时从资产库选择</span>
          )}
        </footer>
      </div>
    </div>
  )
}
