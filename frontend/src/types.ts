export type BrandMode = 'preset' | 'reference' | 'none'
export type Ratio = '1:1' | '9:16' | '4:5' | '16:9' | '3:4' | '4:3'
export type ImageModel =
  | 'gpt-image-2.5-sunburst'
  | 'gemini-3.1-flash-image'
  | 'seedream-5-pro-image-to-image'
  | 'seedream-5.5-image-to-image'
  // Temporary direct MiniMax model until core-ai serves it through the gateway.
  | 'minimax-h3-image'

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
  // The list endpoint ships every reference so the style picker can preview them all.
  reference_images?: BrandReferenceImage[]
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
  merchant_profile: MerchantProfile | null
  schema_version: number
  style_fields: BrandStyleFields | null
  legacy_guides: { layout_guide: string; background_guide: string } | null
  cover_asset_id: string | null
  layout_guide: string
  background_guide: string
  reference_images: BrandReferenceImage[]
}

export type MerchantStore = {
  place_id: string
  name: string
  address: string
  suite: string
  phone: string
}

/** Facts a reader of the style page needs, kept out of the model background. */
export type MerchantBrandInfo = {
  year_established: number | null
  stores: MerchantStore[]
}

export type MerchantProfile = {
  id: string
  name: string
  source_label: string
  source_record_id: string
  sections: { title: string; text: string; source_path: string; original: unknown }[]
  prompt_sections: { title: string; text: string }[]
  brand_info?: MerchantBrandInfo | null
}

export type Asset = {
  id: string
  original_name: string
  width: number
  height: number
  mime_type: string
  url: string
  created_at: string
  source?: 'am' | 'upload'
  read_only?: boolean
  /** AM media library tags; older assets may not carry the field yet. */
  tags?: string[]
}

// ---------- Whole-image flow ----------

export type WholeImageProvider = {
  name: string | null
  model: string | null
  status: string | null
}

export type WholeImageAttachment = {
  asset_id: string
  name: string
  url: string
}

export type WholeImageMessage = {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  created_at: string
  version_id?: string | null
  version?: number | null
  status?: 'done' | 'failed'
  attachments?: WholeImageAttachment[]
}

export type WholeImageModelInput = {
  channel?: 'gateway' | 'agent'
  // 'minimax' marks the temporary direct MiniMax call instead of the core-ai gateway.
  provider?: 'core-ai' | 'minimax'
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

export type WholeImageArtifact = {
  id: string
  kind: 'html' | 'bundle' | 'file'
  file_name: string
  content_type: string
  size: number
  title: string
  preview_url: string | null
  download_url: string | null
}

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
  artifacts?: WholeImageArtifact[]
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
  reference_asset_ids?: string[]
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
    generation_workspace_id?: string | null
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
  operation: 'generate' | 'edit' | 'original_replace'
  replacement_task_id?: string | null
  source_output_id?: string | null
  replacements?: OriginalReplacementItem[]
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
  whole_task_id?: string | null
  id: string
  kind: 'generate' | 'original_replace'
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

// ---------- Original photo replacement ----------

export type OriginalReplacementReference = {
  asset_id: string
  name: string
  preview_url: string
  width: number
  height: number
}

export type OriginalReplacementItem = { target: string; asset_id: string }

export type OriginalReplacementTask = {
  id: string
  whole_task_id?: string | null
  source_version_id?: string | null
  source_output_id?: string | null
  source_version?: number | null
  applied_version_id?: string | null
  applied_version?: number | null
  status: 'queued' | 'running' | 'succeeded' | 'partial' | 'failed' | 'interrupted'
  stage: string
  progress: number
  source: OriginalReplacementReference
  replacements: Array<OriginalReplacementReference & { target: string }>
  output: { asset_id: string; width: number; height: number; preview_url: string; download_url: string } | null
  agent_id: string | null
  run_id: string | null
  agent_input: string | null
  agent_output: string | null
  error: { code: string; message: string } | null
  started_at: string | null
  deadline_at: string | null
  created_at: string
  updated_at: string
}

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
 artifacts?: WholeImageArtifact[]
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


export type MaterialFilterRule = {
  id: string
  text: string
  interpretation: string
}

export type MaterialFilterFolder = {
  id: string
  display_name: string
  original_input: string
  source_root: string
  created_at: string
}

export type MaterialFilterFolderCreateResult = MaterialFilterFolder & { created: boolean }

export type MaterialFilterDecision = 'pass' | 'fail' | 'uncertain' | 'error'

export type MaterialFilterCounters = {
  scanned: number
  added: number
  changed: number
  skipped: number
  passed: number
  failed: number
  uncertain: number
  errors: number
  copied: number
  copy_errors: number
}

export type MaterialFilterRunStatus = 'queued' | 'running' | 'succeeded' | 'interrupted' | 'failed'

export type MaterialFilterRun = {
  id: string
  scheme_id: string
  status: MaterialFilterRunStatus
  limit: number | null
  counters: MaterialFilterCounters
  error: string | null
  item_errors: Array<{ path: string; message: string; at: string }>
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export type MaterialFilterScheme = {
  id: string
  folder_id: string
  name: string
  recursive: boolean
  rules: MaterialFilterRule[]
  output_root: string
  model: string
  prompt_version: string
  active_run_id: string | null
  created_at: string
  last_run?: MaterialFilterRun | null
}

export type MaterialFilterRuleVerdict = {
  id: string
  verdict: 'pass' | 'fail' | 'uncertain'
  reason: string
}

export type MaterialFilterEvaluation = {
  id: string
  scheme_id: string
  file_id: string
  display_name: string
  original_name: string
  relative_path: string
  category: string
  tags: string[]
  decision: MaterialFilterDecision
  model_decision: MaterialFilterDecision | null
  human_decision: 'pass' | 'fail' | null
  effective_decision: MaterialFilterDecision
  manual_pick: boolean
  picked_at: string | null
  description: string
  rules: MaterialFilterRuleVerdict[]
  model: string | null
  source_sha256: string
  source_size: number
  copy_status: 'none' | 'copied' | 'error'
  copy_error: string | null
  uat_asset_id: string | null
  uat_asset_url: string | null
  uat_synced_at: string | null
  uat_sync_error: string | null
  uat_recompressed: boolean
  output_name: string | null
  error: string | null
  preview_url: string | null
  download_url: string | null
  created_at: string
  updated_at: string
}

export type MaterialFilterResultsPage = {
  items: MaterialFilterEvaluation[]
  total: number
  skip: number
  limit: number
  counts: Record<string, number>
}

export type MaterialFilterSync = {
  id: string
  scheme_id: string
  status: 'queued' | 'running' | 'succeeded' | 'partial' | 'failed'
  dry_run: boolean
  counters: {
    total: number
    done: number
    uploaded: number
    compressed: number
    linked: number
    failed: number
  }
  current: string
  error: string | null
  items: Array<{
    name: string
    status: 'uploaded' | 'linked' | 'failed' | 'planned'
    asset_name?: string
    asset_id?: string
    compressed?: boolean
    tag_error?: string | null
    message?: string
    renamed?: boolean
  }>
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export type MaterialFilterDefaults = {
  rules: MaterialFilterRule[]
  model: string
  output_root: string
}
