import { createRoot } from 'react-dom/client'
import AssetPicker from '../../src/components/AssetPicker'
import '../../src/index.css'
import '../../src/styles.css'

createRoot(document.getElementById('root')!).render(
  <AssetPicker open purpose="product" assets={[]} onClose={() => {}} onGoAssets={() => {}} />,
)
