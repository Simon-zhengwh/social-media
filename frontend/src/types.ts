export type AssetKind = 'product' | 'logo'
export type Ratio = '1:1' | '9:16' | '4:5' | '16:9'

export type BrandStyle = {
  id: string
  name: string
  short_name: string
  summary: string
  version: number
  is_default: boolean
  cover_url: string | null
  logo_keywords: string[]
  palette: Record<string, string>
  fonts: Record<string, string>
}

export type Asset = {
  id: string
  kind: AssetKind
  original_name: string
  width: number
  height: number
  mime_type: string
  url: string
  created_at: string
}

export type TaskProvider = {
  name: string | null
  model: string | null
  task_id: string | null
  status: string | null
  progress: number | null
}

export type BackgroundLayer = {
  id: string
  type: 'background'
  name: string
  asset_id: string
  z_index: number
  visible: boolean
  locked: boolean
}

export type TextLayer = {
  id: string
  type: 'text'
  name: string
  role: string
  content: string
  z_index: number
  visible: boolean
  locked: boolean
  position: { x: number; y: number }
  size: { width: number; height: number }
  rotation?: number
  style: {
    font_family?: string
    font_size: number
    font_weight: number
    color: string
    align: 'left' | 'center' | 'right'
    vertical_align?: 'top' | 'middle' | 'bottom'
    line_height: number
    letter_spacing: number
    opacity: number
    stroke: { width: number; color: string }
    shadow: { blur: number; offset_x: number; offset_y: number; color: string }
    background: { color: string; radius: number; padding: number } | null
  }
}

export type LogoLayer = {
  id: string
  type: 'logo'
  name: string
  asset_id: string
  z_index: number
  visible: boolean
  locked: boolean
  position: { x: number; y: number }
  size: { width: number; height: number }
  opacity: number
  rotation: number
  mode: 'model_integrated' | 'overlay'
}

export type Layer = BackgroundLayer | TextLayer | LogoLayer

export type DesignState = {
  schema_version: number
  canvas: { width: number; height: number }
  background: {
    clean_asset_id: string
    render_asset_id: string
    prompt: string
    logo_integrated: boolean
  }
  layers: Layer[]
}

export type Message = {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  created_at: string
  version?: number | null
  actions?: Array<{ type: string; layer_id?: string | null; summary?: string }>
  status?: 'done' | 'failed'
}

export type EditState = {
  status: 'idle' | 'running' | 'failed'
  instruction?: string | null
  error?: string | null
}

export type QualityWarning = {
  code: string
  layer_id?: string | null
  layer_name?: string | null
  message: string
  contrast?: number
}

export type Task = {
  id: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'waiting' | 'interrupted'
  stage: string
  progress: number
  input: {
    product_asset_ids?: string[]
    product_asset_id?: string | null
    logo_asset_id: string | null
    brand_id?: string
    prompt?: string
    ratio?: Ratio
  }
  brand?: {
    id: string | null
    name: string | null
    short_name: string | null
    summary: string | null
  } | null
  canvas: { width: number; height: number }
  provider: TaskProvider
  background_asset_id: string | null
  output_asset_id: string | null
  preview_url: string | null
  download_url: string | null
  design: DesignState | null
  messages: Message[]
  current_version: number
  edit: EditState
  quality: { warnings: QualityWarning[] }
  error: { code: string; message: string } | null
  created_at: string
  updated_at: string
}

export type VersionSummary = {
  version: number
  created_at: string
  instruction: string
  summary: string
  output_asset_id: string
  preview_url: string | null
}

export type LayerPatch = {
  content?: string
  style?: Record<string, unknown>
  position?: { x: number; y: number }
  size?: { width: number; height: number }
  visible?: boolean
  rotation?: number
}

export type SampleImportResult = {
  imported: Asset[]
  skipped: Array<{ file: string; reason: string }>
}
