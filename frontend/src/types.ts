export type AssetKind = 'product' | 'logo' | 'decoration' | 'brand_reference'

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
  contact_policy: 'forbidden' | 'optional'
  default_placement_id: string
  placements: PlatformPlacement[]
}

export type MerchantInfo = { address: string; phone: string }

export type BrandStyle = {
  id: string
  name: string
  short_name: string
  summary: string
  platform: string
  note: string
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

export type SampleImportResult = {
  imported: Asset[]
  skipped: Array<{ file: string; reason: string }>
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

export type WholeImageEditState = {
  status: 'idle' | 'running' | 'failed'
  instruction?: string | null
  source_version_id?: string | null
  error?: string | null
}

export type WholeImageModelInput = {
  endpoint: string
  model: string
  prompt: string
  parameters: {
    size?: string
    quality?: string
    n?: number
    output_format?: string
  }
  images: Array<{
    asset_id: string
    name: string
    kind: string
    preview_url: string | null
  }>
}

export type WholeImageTask = {
  id: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'interrupted'
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
  model_input?: WholeImageModelInput | null
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
