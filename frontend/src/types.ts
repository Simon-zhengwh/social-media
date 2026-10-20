export type AssetKind = 'product' | 'logo' | 'decoration' | 'brand_reference'

export type BrandMode = 'preset' | 'reference' | 'none'
export type Ratio = '1:1' | '9:16' | '4:5' | '16:9' | '3:4' | '4:3'

export type PlatformPlacement = {
  id: string
  name: string
  ratio: Ratio
  width: number
  height: number
  safe_area: { top: number; right: number; bottom: number; left: number }
}

export type PlatformPreset = {
  id: string
  name: string
  summary: string
  contact_policy: 'forbidden' | 'required'
  default_placement_id: string
  placements: PlatformPlacement[]
}

export type MerchantInfo = { address: string; phone: string }

export type BrandStyle = {
  id: string
  name: string
  short_name: string
  summary: string
  is_default: boolean
  cover_url: string | null
  logo_keywords: string[]
}

export type BrandReferenceImage = {
  asset_id: string
  original_name: string
  url: string
  width: number
  height: number
  is_cover: boolean
}

export type BrandDetail = BrandStyle & {
  cover_asset_id: string | null
  layout_guide: string
  background_guide: string
  reference_images: BrandReferenceImage[]
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

export type EditPolicy = {
  ai_locked?: boolean
  manual_edited?: boolean
  created_by?: string
}

export type BackgroundLayer = {
  id: string
  type: 'background'
  name: string
  asset_id: string
  z_index: number
  visible: boolean
  locked: boolean
  edit_policy?: EditPolicy
}

export type TextLayer = {
  id: string
  type: 'text'
  name: string
  role: string
  content: string
  copy_id?: string
  z_index: number
  visible: boolean
  locked: boolean
  position: { x: number; y: number }
  size: { width: number; height: number }
  rotation?: number
  edit_policy?: EditPolicy
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
    runs?: Array<{ text: string; color: string }> | null
  }
}

export type ImageLayer = {
  id: string
  type: 'image'
  name: string
  role: 'logo' | 'product' | 'decoration'
  asset_id: string
  z_index: number
  visible: boolean
  locked: boolean
  position: { x: number; y: number }
  size: { width: number; height: number }
  opacity: number
  rotation: number
  fit: 'contain' | 'cover'
  crop?: { x: number; y: number; width: number; height: number } | null
  render_hint?: 'overlay' | 'baked'
  edit_policy?: EditPolicy
}

export type ShapeLayer = {
  id: string
  type: 'shape'
  name: string
  shape: 'rect' | 'ellipse' | 'polygon' | 'line'
  z_index: number
  visible: boolean
  locked: boolean
  position: { x: number; y: number }
  size: { width: number; height: number }
  opacity: number
  rotation: number
  fill: string | null
  stroke: { width: number; color: string } | null
  radius?: number
  points?: number[][]
  edit_policy?: EditPolicy
}

export type VisualComponentLayer = {
  id: string
  type: 'visual_component'
  name: string
  semantic_type: 'headline' | 'promotion_price' | 'offer_badge' | 'activity_info' | 'brand_lockup'
  render_mode: 'native' | 'regeneratable' | 'flattened'
  content: Record<string, unknown>
  children: Layer[]
  asset_id?: string
  z_index: number
  visible: boolean
  locked: boolean
  position: { x: number; y: number }
  size: { width: number; height: number }
  rotation?: number
  opacity?: number
  style?: Record<string, unknown>
  compositing?: {
    blend_mode?: 'normal' | 'multiply' | 'screen' | 'soft_light'
    opacity?: number
    feather_px?: number
    color_match?: number
    anchor?: string
    shadow?: { blur: number; offset_x: number; offset_y: number; color: string } | null
  }
  relations?: Record<string, unknown>
  edit_policy?: EditPolicy
}

export type Layer = BackgroundLayer | TextLayer | ImageLayer | ShapeLayer | VisualComponentLayer

export type DesignIntent = {
  id: string
  hash: string
  concept: string
  focal_point: string
  composition: string
  palette: string[]
  typography: string
  text_image_relation: string
  decor_strategy: string
  density: string
}

export type DesignState = {
  schema_version: number
  scene_revision?: number
  canvas: { width: number; height: number; color_space?: string }
  renderer?: { engine: string; build_id: string }
  required_capabilities?: string[]
  design_intent?: DesignIntent
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

export type CandidateReview = {
  status: 'not_run' | 'passed' | 'issues_kept' | 'corrected' | 'kept_initial' | 'fallback'
  issues: Array<Record<string, unknown>>
  reason?: string
  candidates: Array<{
    candidate_id: string
    selected: boolean
    issues: number
    hard_failures: number
  }>
}

export type Task = {
  id: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'waiting' | 'interrupted'
  stage: string
  progress: number
  input: {
    product_asset_ids?: string[]
    product_asset_id?: string | null
    brand_id?: string
    brand_mode?: BrandMode
    style_reference_asset_ids?: string[]
    candidate_count?: number
    candidate_batch_id?: string | null
    candidate_index?: number
    prompt?: string
    ratio?: Ratio
    generation_mode?: 'classic' | 'reference_guided'
    platform_id?: string | null
    placement_id?: string | null
    merchant_info?: MerchantInfo | null
  }
  platform?: PlatformPreset | null
  placement?: PlatformPlacement | null
  brand?: {
    id: string | null
    name: string | null
    short_name: string | null
    summary: string | null
  } | null
  canvas: { width: number; height: number }
  provider: TaskProvider
  background_asset_id: string | null
  background_url?: string | null
  design_reference_asset_id?: string | null
  design_reference_url?: string | null
  reference_guided?: {
    enabled: boolean
    status: string
    reason?: string
    reference?: Record<string, unknown>
    components?: Record<string, Record<string, unknown>>
  }
  output_asset_id: string | null
  preview_url: string | null
  download_url: string | null
  design: DesignState | null
  design_intent?: DesignIntent | null
  scene_revision?: number
  renderer?: { engine: string; build_id: string } | null
  review?: CandidateReview | null
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
  content?: string | Record<string, unknown>
  style?: Record<string, unknown>
  compositing?: Record<string, unknown>
  position?: { x: number; y: number }
  size?: { width: number; height: number }
  visible?: boolean
  rotation?: number
  opacity?: number
  asset_id?: string
  fit?: 'contain' | 'cover'
  crop?: { x: number; y: number; width: number; height: number } | null
}

export type SampleImportResult = {
  imported: Asset[]
  skipped: Array<{ file: string; reason: string }>
}

// ---------- Whole-image (non-layered) flow ----------

export type WholeImageProvider = {
  name: string | null
  model: string | null
  status: string | null
}

export type WholeImageMessage = {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  created_at: string
  version_id?: string | null
  version?: number | null
  status?: 'done' | 'failed'
}

export type WholeImageEditState = {
  status: 'idle' | 'running' | 'failed'
  instruction?: string | null
  source_version_id?: string | null
  error?: string | null
}

export type WholeImageTask = {
  id: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'interrupted'
  stage: string
  progress: number
  input: {
    prompt?: string
    ratio?: Ratio
    product_asset_ids?: string[]
    brand_id?: string
    brand_mode?: BrandMode
    style_reference_asset_ids?: string[]
    candidate_count?: number
    candidate_batch_id?: string | null
    candidate_index?: number
    platform_id?: string | null
    placement_id?: string | null
    merchant_info?: MerchantInfo | null
  }
  platform?: PlatformPreset | null
  placement?: PlatformPlacement | null
  brand?: {
    id: string | null
    name: string | null
  }
  canvas: { width: number; height: number }
  provider: WholeImageProvider
  selected_version_id: string | null
  current_version_id: string | null
  current_version: number
  preview_url: string | null
  download_url: string | null
  messages: WholeImageMessage[]
  edit: WholeImageEditState
  error: { code: string; message: string } | null
  created_at: string
  updated_at: string
}

export type WholeImageVersion = {
  id: string
  version: number
  parent_version_id: string | null
  operation: 'generate' | 'edit'
  instruction: string
  width: number
  height: number
  output_asset_id: string | null
  preview_url: string | null
  download_url: string | null
  created_at: string
}
