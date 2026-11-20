import type {
  BrandMode,
  PosterSeriesGroup,
  PosterSeriesMode,
  PosterSeriesPage,
  PosterSeriesRunPayload,
  SeriesLogoPosition,
  PosterSeriesWorkspace,
  Ratio,
} from './types'
import type { ModelCounts } from './modelGeneration'

/**
 * Pure helpers for the opt-in poster series workbench.
 *
 * The series tree only adds planning, cover locking and group downloads around ordinary
 * whole-image tasks, so everything here is either a derivation of the server snapshot or
 * a payload builder. Nothing in this module touches the network.
 */

export const SERIES_STATUS_LABEL: Record<string, string> = {
  planned: '待生成',
  pending: '待生成',
  queued: '排队中',
  running: '生成中',
  succeeded: '已完成',
  complete: '已完成',
  failed: '失败',
}

export function seriesStatusLabel(status: string | null | undefined): string {
  const value = String(status ?? '')
  return SERIES_STATUS_LABEL[value] ?? (value || '待生成')
}

export function seriesPageUsable(page: PosterSeriesPage | null): boolean {
  return Boolean(page && page.status === 'succeeded' && (page.preview_url || page.asset_id))
}

export function seriesPageLabel(page: PosterSeriesPage): string {
  return `图片 ${page.order + 1}`
}

/** File-name safe page label used by the group ZIP, kept in sync with the backend order. */
export function seriesPageFileName(page: PosterSeriesPage): string {
  const number = String(Math.max(1, page.order + 1)).padStart(2, '0')
  return `${number}-${page.role === 'cover' ? '封面' : `介绍页${page.order}`}.png`
}

export type SeriesSelection = { group: PosterSeriesGroup; page: PosterSeriesPage }

export function resolveSeriesSelection(
  workspace: PosterSeriesWorkspace | null,
  groupId: string | null,
  pageId: string | null,
): SeriesSelection | null {
  const groups = workspace?.groups ?? []
  if (!groups.length) return null
  const group = groups.find((item) => item.id === groupId)
    ?? groups.find((item) => item.pages.some((page) => page.page_id === pageId))
    ?? groups[0]
  const page = group.pages.find((item) => item.page_id === pageId) ?? defaultGroupPage(group)
  return page ? { group, page } : null
}

/** Deep links and the browser URL carry the group/page so a reload lands on the same page. */
export function selectionFromUrl(search: string): { groupId: string | null; pageId: string | null } {
  const params = new URLSearchParams(search)
  return { groupId: params.get('series_group'), pageId: params.get('series_page') }
}

/** The first available image in the Agent's output order. */
export function defaultGroupPage(group: PosterSeriesGroup): PosterSeriesPage | null {
  return group.pages[0] ?? null
}

/** Deep links from the task center only carry a page task id, so the page has to be found. */
export function selectionForTask(
  workspace: PosterSeriesWorkspace | null,
  taskId: string | null,
): SeriesSelection | null {
  if (!workspace || !taskId) return null
  for (const group of workspace.groups) {
    const page = group.pages.find((item) => item.whole_task_id === taskId)
    if (page) return { group, page }
  }
  return null
}

/** Patch one group from a POST response without re-fetching the whole snapshot. */
export function mergeSeriesGroup(
  workspace: PosterSeriesWorkspace | null,
  group: PosterSeriesGroup,
): PosterSeriesWorkspace | null {
  if (!workspace) return workspace
  return { ...workspace, groups: workspace.groups.map((item) => item.id === group.id ? group : item) }
}

export function mergeSeriesPage(
  workspace: PosterSeriesWorkspace | null,
  page: PosterSeriesPage,
): PosterSeriesWorkspace | null {
  if (!workspace) return workspace
  return {
    ...workspace,
    groups: workspace.groups.map((group) => group.id === page.group_id
      ? { ...group, pages: group.pages.map((item) => item.page_id === page.page_id ? page : item) }
      : group),
  }
}

export function seriesNeedsPoll(workspace: PosterSeriesWorkspace | null): boolean {
  return Boolean(workspace?.runs.some(run => run.workflow === 'agent-series' && (run.status === 'queued' || run.status === 'running'))
    || workspace?.groups.some((group) =>
    (!group.pages.length && (group.status === 'queued' || group.status === 'running'))
    || group.pages.some((page) => page.status === 'queued' || page.status === 'running')))
}

export function pageVersionRef(page: PosterSeriesPage | null): { versionId: string | null; outputId: string | null } {
  return { versionId: page?.version_id ?? null, outputId: page?.output_id ?? null }
}

/** Only a fully succeeded group with a selected output on every page can be zipped. */
export function groupDownloadHref(group: PosterSeriesGroup): string | null {
  return group.can_download ? group.id : null
}

export function groupDownloadReady(group: PosterSeriesGroup): boolean {
  return Boolean(groupDownloadHref(group))
}

/** A finished page can always be downloaded on its own, even while its siblings failed. */
export function pageDownloadHref(page: PosterSeriesPage): string | null {
  return page.download_url ?? (page.asset_id ? 'page' : null)
}

/** Replacement defaults come from the current version's content refs, never the pool. */
export function replacementDefaults(page: PosterSeriesPage | null): Array<{ asset_id: string; target: string }> {
  return (page?.content_refs ?? [])
    .filter((ref) => Boolean(ref?.asset_id))
    .map((ref) => ({ asset_id: ref.asset_id, target: ref.target ?? '' }))
}

export function seriesContentPool(group: PosterSeriesGroup | null) {
  return (group?.content_pool ?? []).map(item => typeof item === 'string' ? { asset_id: item } : item)
}

/**
 * The "full asset pool" for a page is every image this task can still substitute in: the
 * round's uploaded pool when the server exposes it, otherwise every image the series pages
 * already reference, plus whatever the page task itself carried.
 */
export function taskAssetPool(
  workspace: PosterSeriesWorkspace | null,
  group: PosterSeriesGroup | null,
  extra: string[] = [],
): string[] {
  const ids: string[] = [
    ...seriesContentPool(group).map(item => item.asset_id),
    ...(workspace?.groups ?? []).flatMap((item) => item.pages.flatMap((page) => [
      ...(page.content_refs ?? []).map((ref) => ref.asset_id),
      ...(page.style_reference_ids ?? []),
    ])),
    ...extra,
  ]
  return [...new Set(ids.filter((id) => Boolean(id)))]
}

/** Replacements exclude the group cover reference, style references and Logo images. */
export function replacementPool(page: PosterSeriesPage | null, pool: string[]): string[] {
  const excluded = new Set<string>([
    ...(page?.style_reference_ids ?? []),
    ...(page?.cover_reference?.asset_id ? [page.cover_reference.asset_id] : []),
  ])
  const seen = new Set<string>()
  return pool.filter((id) => {
    if (!id || excluded.has(id) || seen.has(id)) return false
    seen.add(id)
    return true
  })
}

export function modelGroupPlan(counts: ModelCounts): Array<{ image_model: PosterSeriesRunPayload['image_groups'][number]['image_model']; group_count: number }> {
  return Object.entries(counts)
    .filter(([, count]) => typeof count === 'number' && count > 0)
    .map(([model, count]) => ({
      image_model: model as PosterSeriesRunPayload['image_groups'][number]['image_model'],
      group_count: Math.min(6, Math.max(1, Math.trunc(count as number))),
    }))
}

export type SeriesSubmitInput = {
  submitId: string
  prompt: string
  promptOptimized: boolean
  ratio?: Ratio
  products: string[]
  brandId: string | null
  brandMode: BrandMode
  logoAssetId: string | null
  logoPosition?: SeriesLogoPosition | null
  styleReferenceIds: string[]
  modelCounts: ModelCounts
  title?: string
  platformId: string | null
  placementId: string | null
  includeStoreAddress: boolean
  includeStorePhone: boolean
  storePlaceId: string | null
}

/**
 * One submit sends the selected groups and the user's requirement to the Agent.
 */
export function buildSeriesRunPayload(input: SeriesSubmitInput): PosterSeriesRunPayload {
  return {
    submit_id: input.submitId,
    prompt: input.prompt,
    prompt_optimized: input.promptOptimized,
    ...(input.ratio ? { ratio: input.ratio } : {}),
    reference_asset_ids: input.products,
    brand_id: input.brandId,
    brand_mode: input.brandMode,
    logo_asset_id: input.logoAssetId,
    ...(input.logoAssetId && input.logoPosition ? { logo_position: input.logoPosition } : {}),
    style_reference_asset_ids: input.styleReferenceIds,
    image_groups: modelGroupPlan(input.modelCounts),
    workspace_title: input.title,
    platform_id: input.platformId,
    placement_id: input.placementId,
    include_store_address: input.includeStoreAddress,
    include_store_phone: input.includeStorePhone,
    store_place_id: input.storePlaceId,
  }
}

export type WorkspaceMode = { workspace_id: string; mode: PosterSeriesMode; locked: boolean; series_enabled: boolean }

/**
 * The first accepted submission locks the mode. Before that the operator's choice stands,
 * and a workspace that reports a locked mode always wins over a stale draft choice.
 */
export function modeAfterServer(
  local: { mode: PosterSeriesMode; locked: boolean },
  server: WorkspaceMode | null,
  draftMode: PosterSeriesMode | null,
): { mode: PosterSeriesMode; locked: boolean } {
  if (!server) return local
  if (server.locked) return { mode: server.mode, locked: true }
  return { mode: local.locked ? local.mode : draftMode ?? local.mode, locked: false }
}

export function canStartSeries(options: {
  configEnabled: boolean
  mode: PosterSeriesMode
  locked: boolean
  hasPages: boolean
}): boolean {
  return options.configEnabled && options.mode === 'series' && !options.locked && !options.hasPages
}

export function seriesGroupTitle(_group: PosterSeriesGroup, index: number): string {
  return `组 ${index + 1}`
}

export function groupProgressLabel(group: PosterSeriesGroup): string {
  return `${group.succeeded_count}/${group.page_count} 页`
}

export function seriesRoundLabel(workspace: PosterSeriesWorkspace | null, group: PosterSeriesGroup): string {
  const run = workspace?.runs.find((item) => item.id === group.run_id)
  return run ? `第 ${run.round} 轮` : ''
}

export function uniqueRuns(workspace: PosterSeriesWorkspace | null): PosterSeriesWorkspace['runs'] {
  return workspace?.runs ?? []
}

/* --------------------------------------------------------------------------------------
   HTML editor host
   -------------------------------------------------------------------------------------- */

export const SERIES_EDITOR_EXPORT = 'poster-series-editor-export'
export const SERIES_EDITOR_EXPORT_REQUEST = 'poster-series-export-request'
export const SERIES_EDITOR_READY = 'poster-series-editor-ready'
export const SERIES_EDITOR_ERROR = 'poster-series-editor-error'

/**
 * The published editor templates keep their export inside an IIFE (``(()=>{...})()``), so
 * there is no global canvas function to call. The bridge therefore drives the template's own
 * "download current view" button and intercepts the resulting PNG blob before the browser
 * can start a download: the exported bytes are exactly what the template produces.
 */
export function editorBridgeScript(sessionId: string): string {
  const session = JSON.stringify(sessionId)
  return `<script id="poster-series-save-bridge">(function(){
  var SESSION=${session};
  var EXPORT=${JSON.stringify(SERIES_EDITOR_EXPORT)};
  var REQUEST=${JSON.stringify(SERIES_EDITOR_EXPORT_REQUEST)};
  var READY=${JSON.stringify(SERIES_EDITOR_READY)};
  var ERROR=${JSON.stringify(SERIES_EDITOR_ERROR)};
  var pending=null;
  var captured=null;
  function send(type,extra){
    var payload={type:type,session_id:SESSION};
    if(extra){for(var key in extra){if(Object.prototype.hasOwnProperty.call(extra,key))payload[key]=extra[key];}}
    try{parent.postMessage(payload,'*');}catch(error){}
  }
  function fail(text){
    var request=pending;pending=null;captured=null;
    send(ERROR,{request_id:request?request.id:null,message:String(text||'编辑器导出失败')});
  }
  function emit(png,width,height){
    var request=pending;pending=null;captured=null;
    send(EXPORT,{request_id:request?request.id:null,png:png,width:width,height:height});
  }
  var nativeCreate=window.URL&&typeof window.URL.createObjectURL==='function'
    ?window.URL.createObjectURL.bind(window.URL):null;
  if(nativeCreate){
    window.URL.createObjectURL=function(value){
      try{if(value&&String(value.type||'').indexOf('image/png')===0)captured=value;}catch(error){}
      return nativeCreate(value);
    };
  }
  function measure(png){
    var image=new Image();
    image.onload=function(){emit(png,image.naturalWidth,image.naturalHeight);};
    image.onerror=function(){fail('编辑器导出的图片无法读取');};
    image.src=png;
  }
  function readBlob(blob){
    var reader=new FileReader();
    reader.onload=function(){
      var png=String(reader.result||'');
      if(png.indexOf('data:image/png;base64,')!==0){fail('编辑器导出的内容不是 PNG');return;}
      measure(png);
    };
    reader.onerror=function(){fail('读取编辑器导出的图片失败');};
    reader.readAsDataURL(blob);
  }
  // A template export always ends in an <a download> click; keep it from touching the disk.
  if(window.HTMLAnchorElement&&window.HTMLAnchorElement.prototype){
    var nativeClick=window.HTMLAnchorElement.prototype.click;
    window.HTMLAnchorElement.prototype.click=function(){
      var name=this.getAttribute&&this.getAttribute('download');
      if(pending&&name&&/\.png$/i.test(name)){
        var blob=captured;
        if(blob){readBlob(blob);return;}
        var href=this.getAttribute('href')||'';
        if(href.indexOf('data:image/png;base64,')===0){measure(href);return;}
        fail('没有捕获到编辑器导出的图片');
        return;
      }
      return nativeClick.apply(this,arguments);
    };
  }
  function exportButton(){
    var ids=['download','download-poster','export-png','export'];
    for(var index=0;index<ids.length;index++){
      var byId=document.getElementById(ids[index]);
      if(byId)return byId;
    }
    var buttons=document.querySelectorAll('button,a');
    for(var index2=0;index2<buttons.length;index2++){
      var text=String(buttons[index2].textContent||'');
      if(text.indexOf('下载')>=0&&text.indexOf('PNG')>=0)return buttons[index2];
    }
    return null;
  }
  function startExport(requestId){
    pending={id:requestId||null};
    captured=null;
    var button=exportButton();
    if(!button){fail('当前编辑器没有可用的导出按钮');return;}
    var attempts=0;
    var timer=window.setInterval(function(){
      if(!pending){window.clearInterval(timer);return;}
      if(button.disabled){
        attempts+=1;
        if(attempts>24){window.clearInterval(timer);fail('编辑器尚未准备好导出');}
        return;
      }
      window.clearInterval(timer);
      try{button.click();}catch(error){fail(error&&error.message);}
      window.setTimeout(function(){
        if(!pending)return;
        // Some templates export the current canvas through a global helper instead.
        try{
          if(typeof window.makeCanvas==='function'){
            var canvas=window.makeCanvas();
            if(canvas&&canvas.width&&canvas.height){
              emit(canvas.toDataURL('image/png'),canvas.width,canvas.height);
              return;
            }
          }
        }catch(error){}
        fail('编辑器没有返回可导出的画布，请确认页面已完全加载');
      },4000);
    },250);
  }
  window.addEventListener('message',function(event){
    var data=event.data;
    if(!data||typeof data!=='object'||data.type!==REQUEST)return;
    if(data.session_id&&data.session_id!==SESSION)return;
    startExport(data.request_id||null);
  });
  function announce(){send(READY,{});}
  if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',announce);}
  else{announce();}
})();</script>`
}

/**
 * The artifact preview is served with a sandbox CSP; an ``srcdoc`` document inherits the
 * parent's policy instead, so the composed document installs the same restrictions itself.
 */
export const SERIES_EDITOR_CSP =
  "default-src 'none'; img-src data: blob:; script-src 'unsafe-inline'; style-src 'unsafe-inline'; " +
  "font-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'"

export function editorCspMeta(): string {
  return `<meta http-equiv="Content-Security-Policy" content="${SERIES_EDITOR_CSP}">`
}

/** Put the CSP and the bridge in front of the template's own scripts. */
export function composeEditorDocument(html: string, sessionId: string): string {
  const meta = editorCspMeta()
  const script = editorBridgeScript(sessionId)
  const lower = html.toLowerCase()
  const headAt = lower.indexOf('<head')
  if (headAt >= 0) {
    const headEnd = lower.indexOf('>', headAt)
    if (headEnd >= 0) return `${html.slice(0, headEnd + 1)}${meta}${script}${html.slice(headEnd + 1)}`
  }
  return `${meta}${script}${html}`
}

/** Inject the host bridge into the fetched editor HTML without touching anything else. */
export function injectEditorBridge(html: string, sessionId: string): string {
  return composeEditorDocument(html, sessionId)
}

export type EditorMessageExpectation = {
  sessionId: string
  width: number
  height: number
}

export type EditorMessageResult =
  | { kind: 'ignore' }
  | { kind: 'ready' }
  | { kind: 'error'; message: string }
  | { kind: 'export'; base64: string; width: number; height: number }

function pngBase64(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const match = /^data:image\/png;base64,([A-Za-z0-9+/=]+)$/.exec(value.trim())
  if (match) return match[1]
  if (/^[A-Za-z0-9+/=]+$/.test(value) && value.length > 64) return value
  return null
}

/**
 * Validate one postMessage from the isolated editor: only the host session, only PNG data and
 * only the group canvas size are accepted.
 */
export function parseEditorMessage(data: unknown, expected: EditorMessageExpectation): EditorMessageResult {
  if (!data || typeof data !== 'object') return { kind: 'ignore' }
  const message = data as Record<string, unknown>
  const type = String(message.type ?? '')
  if (![SERIES_EDITOR_READY, SERIES_EDITOR_ERROR, SERIES_EDITOR_EXPORT].includes(type)) return { kind: 'ignore' }
  if (message.session_id !== expected.sessionId) return { kind: 'ignore' }
  if (type === SERIES_EDITOR_READY) return { kind: 'ready' }
  if (type === SERIES_EDITOR_ERROR) return { kind: 'error', message: String(message.message || '编辑器导出失败') }
  const base64 = pngBase64(message.png)
  if (!base64) return { kind: 'error', message: '编辑器返回的不是 PNG 图片' }
  const width = Number(message.width)
  const height = Number(message.height)
  if (!Number.isFinite(width) || !Number.isFinite(height) || width !== expected.width || height !== expected.height) {
    return { kind: 'error', message: `编辑器画布 ${width}×${height} 与页面画布不一致` }
  }
  return { kind: 'export', base64, width, height }
}

export type HtmlSaveBody = {
  source_version_id: string
  expected_output_id: string | null
  artifact_id: string | null
  session_id: string
  png_base64: string
  width: number
  height: number
  content_refs: Array<{ asset_id: string; target: string }>
}

export function htmlSaveBody(options: {
  sessionId: string
  sourceVersionId: string
  expectedOutputId: string | null
  artifactId: string | null
  base64: string
  width: number
  height: number
  contentRefs: Array<{ asset_id: string; target: string }>
}): HtmlSaveBody {
  return {
    source_version_id: options.sourceVersionId,
    expected_output_id: options.expectedOutputId,
    artifact_id: options.artifactId,
    session_id: options.sessionId,
    png_base64: options.base64,
    width: options.width,
    height: options.height,
    content_refs: options.contentRefs,
  }
}

export function editorConflictMessage(error: { status?: number; message?: string } | null): string {
  if (error?.status === 409) return error.message || '这一页已切换到新版本，请重新打开编辑器再保存'
  if (error?.status === 422) return error.message || '编辑器结果不符合当前画布，未保存'
  return error?.message || '保存到本页失败'
}
