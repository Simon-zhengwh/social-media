import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import WholeArtifactPreview from '../../src/components/WholeArtifactPreview'
import '../../src/styles.css'

const projectId = new URLSearchParams(location.search).get('project_id') || ''
const preview = `/api/merchants/m1/whole-image/editable-projects/${encodeURIComponent(projectId)}/preview`
export default function Fixture() {
  const [open, setOpen] = useState(true)
  return <><button onClick={() => setOpen(true)}>重新打开工程</button><WholeArtifactPreview projectId={open ? projectId : undefined} artifact={open ? {
    id: projectId, kind: 'html', file_name: 'editor.html', title: '可编辑海报测试', content_type: 'text/html', size: 0,
    preview_url: preview, download_url: null,
  } : null} onClose={() => setOpen(false)} /></>
}
createRoot(document.getElementById('root')!).render(<Fixture />)
