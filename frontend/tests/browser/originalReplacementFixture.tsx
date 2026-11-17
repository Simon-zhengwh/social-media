import { createRoot } from 'react-dom/client'
import WholeImageStudio from '../../src/components/WholeImageStudio'
import { ImageLightboxProvider } from '../../src/components/ImageLightbox'
import type { Asset, BrandLogoImage } from '../../src/types'
import '../../src/styles.css'

const data: { assets: Asset[]; logos: BrandLogoImage[] } = await fetch('/fixture/data').then(response => response.json())
createRoot(document.getElementById('root')!).render(
  <ImageLightboxProvider><WholeImageStudio userId="fixture" active newDesignHref="#" brandStudioHref="#"
    merchantProfile={null} brands={[]} merchantLogos={data.logos} brandsLoading={false} brandsError={null}
    platforms={[]} platformsLoading={false} platformsError={null} assets={data.assets} assetsLoading={false}
    onLoadAssets={() => {}} onGoAssets={() => {}} /></ImageLightboxProvider>,
)
