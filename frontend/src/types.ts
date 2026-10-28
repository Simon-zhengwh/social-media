export type BrandMode = 'preset' | 'reference' | 'none'
export type Ratio = '1:1' | '9:16' | '4:5' | '16:9' | '3:4' | '4:3'
export type ImageModel =
  | 'gpt-image-2.5-sunburst'
  | 'gemini-3.1-flash-image'
  | 'seedream-5-pro-image-to-image'

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
  default_placement_id: string
  placements: PlatformPlacement[]
  content_rules: string[]
}

export type BrandColor = {
  hex: string
}

export type BrandLogoImage = {
  asset_id: string
  original_name: string
  url: string
  width: number
  height: number
}

export type BrandStyle = {
  id: string
  name: string
  short_name: string
  summary: string
  platform: string
  platform_id: string
  placement_id: string
  purpose: string
  note: string
  is_default: boolean
  cover_url: string | null
  logo_keywords: string[]
  colors: BrandColor[]
  logo_asset_ids: string[]
  logo_images: BrandLogoImage[]
}

export type BrandReferenceImage = {
  asset_id: string
  original_name: string
  url: string
  width: number
  height: number
  is_cover: boolean
}

export type BrandStyleFields = {
  palette_notes: string
  typography: string
  subject_style: string
  background: string
  layout: string
  decoration: string
  identity: string
  restrictions: string
}

export type BrandRules = {
  version: number
  copy_accuracy: string
  asset_usage: string
  logo_usage: string
  readability: string
}

export type BrandDetail = BrandStyle & {
  schema_version: number
  style_fields: BrandStyleFields | null
  legacy_guides: { layout_guide: string; background_guide: string } | null
  cover_asset_id: string | null
  layout_guide: string
  background_guide: string
  reference_images: BrandReferenceImage[]
}

export type Asset = {
  id: string
  original_name: string
  width: number
  height: number
  mime_type: string
  url: string
  created_at: string
}

// ---------- Whole-image flow ----------

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

export type WholeImageModelInput = {
  channel?: 'gateway' | 'agent'
  endpoint: string
  model: string
  prompt: string
  parameters: {
    size?: string
    quality?: string
    n?: number
    output_format?: string
    agent_id?: string
    timeout_seconds?: number
    image_url?: string
  }
  images: Array<{
    asset_id: string
    name: string
    role: string
    kind?: string
    url?: string | null
    preview_url: string | null
  }>
}

export type WholeImageTaskStatus =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'interrupted'

export type WholeImageOutput = {
  id: string
  channel: 'gateway' | 'agent'
  role: string
  label: string
  asset_id: string
  width: number
  height: number
  selectable: boolean
  preview_url: string | null
  download_url: string | null
  model_input: WholeImageModelInput | null
}

export type WholeImageBranch = {
  status: 'idle' | 'preparing' | 'running' | 'completed' | 'failed'
  started_at?: string | null
  completed_at?: string | null
  run_id?: string | null
  error?: string | null
  model_input: WholeImageModelInput | null
  outputs: WholeImageOutput[]
}

export type WholeImageEditState = {
  status: 'idle' | 'running' | 'awaiting_selection' | 'failed'
  instruction?: string | null
  source_version_id?: string | null
  source_output_id?: string | null
  outcome?: 'success' | 'partial' | 'failed' | null
  error?: string | null
  branches?: Partial<Record<'gateway' | 'agent', WholeImageBranch>>
}

export type WholeImageTask = {
  id: string
  status: WholeImageTaskStatus
  stage: string
  progress: number
  input: {
    image_model?: ImageModel
    prompt?: string
    prompt_optimized?: boolean
    ratio?: Ratio
    product_asset_ids?: string[]
    brand_id?: string
    brand_mode?: BrandMode
    logo_asset_id?: string | null
    style_reference_asset_ids?: string[]
    candidate_count?: number
    candidate_batch_id?: string | null
    candidate_index?: number
    platform_id?: string | null
    placement_id?: string | null
  }
  platform?: PlatformPreset | null
  placement?: PlatformPlacement | null
  brand?: {
    id: string | null
    name: string | null
    purpose?: string | null
    platform_id?: string | null
  }
  canvas: { width: number; height: number }
  provider: WholeImageProvider
  model_input?: WholeImageModelInput | null
  model_inputs?: Array<WholeImageModelInput | null>
  outputs: WholeImageOutput[]
  current_output_id: string | null
  requires_output_selection: boolean
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
  outputs: WholeImageOutput[]
  model_input?: WholeImageModelInput | null
  created_at: string
}

export type WholeImageTaskGroupStatus = 'active' | 'ready' | 'failed'

export type WholeImageTaskGroupFilter = 'all' | WholeImageTaskGroupStatus

export type WholeImageTaskGroupCandidate = {
  id: string
  candidate_index: number
  status: WholeImageTaskStatus
  stage: string
  progress: number
  current_version: number
  preview_url: string | null
  download_url: string | null
  edit_status: 'idle' | 'running' | 'failed'
  error: string | null
  created_at: string
  updated_at: string
}

export type WholeImageTaskGroup = {
  id: string
  kind: 'generate' | 'edit'
  status: WholeImageTaskGroupStatus
  prompt: string
  brand_name: string | null
  platform_name: string | null
  placement_name: string | null
  canvas: { width: number; height: number }
  candidate_count: number
  active_count: number
  succeeded_count: number
  failed_count: number
  retryable_count: number
  representative_task_id: string
  preview_url: string | null
  download_url: string | null
  error_message: string | null
  candidates: WholeImageTaskGroupCandidate[]
  created_at: string
  updated_at: string
}

export type WholeImageTaskGroupPage = {
  items: WholeImageTaskGroup[]
  next_cursor: string | null
  total: number
}

// ---------- Direct poster editing ----------

export type PosterEditReference = {
  asset_id: string
  name: string
  preview_url: string
  width: number
  height: number
}

export type PosterEditOutput = {
  id: string
  role: string
  label: string
  asset_id: string
  width: number
  height: number
  selectable: boolean
  preview_url: string | null
  download_url: string | null
  model_input: string | null
}

export type PosterEditTask = {
  id: string
  batch_id: string
  candidate_index: number
  candidate_count: number
  status: 'queued' | 'running' | 'succeeded' | 'partial' | 'failed' | 'interrupted'
  stage: string
  progress: number
  prompt: string
  references: PosterEditReference[]
  outputs: PosterEditOutput[]
  agent_id: string
  agent_input: string | null
  run_id: string | null
  error: { code: string; message: string } | null
  created_at: string
  updated_at: string
}


export type PosterEditBatch = {
  id: string
  candidate_count: number
  tasks: PosterEditTask[]
  created_at: string
  updated_at: string
}
