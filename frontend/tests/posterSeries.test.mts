import assert from 'node:assert/strict'
import { beforeEach, test } from 'node:test'
import {
  SERIES_EDITOR_ERROR,
  SERIES_EDITOR_EXPORT,
  SERIES_EDITOR_EXPORT_REQUEST,
  SERIES_EDITOR_CSP,
  SERIES_EDITOR_READY,
  buildSeriesRunPayload,
  canStartSeries,
  composeEditorDocument,
  defaultGroupPage,
  editorBridgeScript,
  editorConflictMessage,
  groupDownloadReady,
  groupProgressLabel,
  htmlSaveBody,
  injectEditorBridge,
  mergeSeriesGroup,
  mergeSeriesPage,
  modeAfterServer,
  modelGroupPlan,
  pageDownloadHref,
  parseEditorMessage,
  replacementDefaults,
  replacementPool,
  resolveSeriesSelection,
  selectionForTask,
  selectionFromUrl,
  seriesNeedsPoll,
  seriesPageFileName,
  seriesPageLabel,
  seriesStatusLabel,
  taskAssetPool,
} from '../src/posterSeries.ts'
import * as seriesModule from '../src/posterSeries.ts'
import { readPosterSession, savePosterSession } from '../src/posterSession.ts'
import type {
  PosterSeriesGroup,
  PosterSeriesPage,
  PosterSeriesWorkspace,
} from '../src/types.ts'

class MemoryStorage {
  data = new Map<string, string>()
  getItem(key: string) { return this.data.get(key) ?? null }
  setItem(key: string, value: string) { this.data.set(key, value) }
  removeItem(key: string) { this.data.delete(key) }
}

beforeEach(() => {
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: { sessionStorage: new MemoryStorage(), location: { search: '', href: 'https://example.test/?merchant_id=shop' } },
  })
})

function page(values: Partial<PosterSeriesPage> & { page_id: string; order: number }): PosterSeriesPage {
  return {
    group_id: 'group-a',
    run_id: 'run-1',
    role: values.order === 0 ? 'cover' : 'detail',
    status: 'planned',
    whole_task_id: null,
    version_id: null,
    version: 0,
    output_id: null,
    asset_id: null,
    preview_url: null,
    thumb_url: null,
    download_url: null,
    error: null,
    content_refs: [],
    style_reference_ids: [],
    cover_reference: null,
    logo_box: null,
    ...values,
  }
}

function group(values: Partial<PosterSeriesGroup> & { id: string; order: number }): PosterSeriesGroup {
  return {
    run_id: 'run-1',
    workspace_id: 'workspace-1',
    image_model: 'gpt-image-2.5-sunburst',
    page_count: 2,
    status: 'planned',
    cover_page_id: null,
    cover_ready: false,
    interiors_started_at: null,
    auto_generate_interiors: false,
    complete: false,
    can_download: false,
    succeeded_count: 0,
    failed_count: 0,
    running_count: 0,
    canvas: { width: 1080, height: 1920 },
    pages: [],
    ...values,
  }
}

function workspace(groups: PosterSeriesGroup[]): PosterSeriesWorkspace {
  return {
    workspace_id: 'workspace-1',
    mode: 'series',
    locked: true,
    series_enabled: true,
    runs: [{
      id: 'run-1',
      workspace_id: 'workspace-1',
      round: 1,
      status: 'running',
      prompt: '每组 3 张',
      pages_per_group: 3,
      auto_generate_interiors: false,
      canvas: { width: 1080, height: 1920 },
      created_at: '2026-11-18T00:00:00Z',
    }],
    groups,
  }
}

test('page and status labels stay readable for planned, running, failed and done pages', () => {
  assert.equal(seriesStatusLabel('planned'), '待生成')
  assert.equal(seriesStatusLabel('running'), '生成中')
  assert.equal(seriesStatusLabel('succeeded'), '已完成')
  assert.equal(seriesStatusLabel('failed'), '失败')
  assert.equal(seriesStatusLabel('mystery'), 'mystery')
  assert.equal(seriesPageLabel(page({ page_id: 'p1', order: 0 })), '图片 1')
  assert.equal(seriesPageLabel(page({ page_id: 'p2', order: 2 })), '图片 3')
  assert.equal(seriesPageFileName(page({ page_id: 'p1', order: 0 })), '01-封面.png')
  assert.equal(seriesPageFileName(page({ page_id: 'p2', order: 1 })), '02-介绍页1.png')
})

test('a deep link restores its group and page, otherwise the first cover opens', () => {
  const groups = [
    group({ id: 'group-a', order: 0, pages: [page({ page_id: 'a1', order: 0 }), page({ page_id: 'a2', order: 1 })] }),
    group({ id: 'group-b', order: 1, image_model: 'seedream-5.5-image-to-image',
      pages: [page({ page_id: 'b1', order: 0, group_id: 'group-b' }), page({ page_id: 'b2', order: 1, group_id: 'group-b' })] }),
  ]
  const snapshot = workspace(groups)
  const linked = resolveSeriesSelection(snapshot, 'group-b', 'b2')
  assert.equal(linked?.group.id, 'group-b')
  assert.equal(linked?.page.page_id, 'b2')
  // A page id alone still finds its group.
  assert.equal(resolveSeriesSelection(snapshot, null, 'b2')?.group.id, 'group-b')
  // A stale selection falls back to the first group's cover.
  assert.equal(resolveSeriesSelection(snapshot, 'gone', 'gone')?.page.page_id, 'a1')
  assert.equal(resolveSeriesSelection(null, 'group-a', 'a1'), null)
  assert.equal(defaultGroupPage(groups[1])?.page_id, 'b1')
  assert.deepEqual(selectionFromUrl('?series_group=group-b&series_page=b2'), { groupId: 'group-b', pageId: 'b2' })
  assert.deepEqual(selectionFromUrl(''), { groupId: null, pageId: null })
})

test('task-center links open the page that owns the task', () => {
  const snapshot = workspace([
    group({ id: 'group-a', order: 0, pages: [page({ page_id: 'a1', order: 0, whole_task_id: 'task-a1' })] }),
    group({ id: 'group-b', order: 1, pages: [page({ page_id: 'b2', order: 1, group_id: 'group-b', whole_task_id: 'task-b2' })] }),
  ])
  assert.equal(selectionForTask(snapshot, 'task-b2')?.page.page_id, 'b2')
  assert.equal(selectionForTask(snapshot, 'unknown'), null)
  assert.equal(selectionForTask(snapshot, null), null)
})

test('the workspace mode locks to the server mode after the first accepted submission', () => {
  const locked = modeAfterServer({ mode: 'series', locked: false }, {
    workspace_id: 'w', mode: 'single', locked: true, series_enabled: true,
  }, 'series')
  assert.deepEqual(locked, { mode: 'single', locked: true })
  const unlocked = modeAfterServer({ mode: 'single', locked: false }, {
    workspace_id: 'w', mode: 'single', locked: false, series_enabled: true,
  }, 'series')
  assert.deepEqual(unlocked, { mode: 'series', locked: false })
  assert.deepEqual(modeAfterServer({ mode: 'single', locked: false }, null, 'series'), { mode: 'single', locked: false })
  assert.equal(canStartSeries({ configEnabled: true, mode: 'series', locked: false, hasPages: false }), true)
  assert.equal(canStartSeries({ configEnabled: false, mode: 'series', locked: false, hasPages: false }), false)
  assert.equal(canStartSeries({ configEnabled: true, mode: 'series', locked: true, hasPages: false }), false)
  assert.equal(canStartSeries({ configEnabled: true, mode: 'series', locked: false, hasPages: true }), false)
})

test('model counts are group counts and the page count stays in the requirement', () => {
  const plan = modelGroupPlan({ 'gpt-image-2.5-sunburst': 2, 'seedream-5.5-image-to-image': 1 })
  assert.deepEqual(plan, [
    { image_model: 'gpt-image-2.5-sunburst', group_count: 2 },
    { image_model: 'seedream-5.5-image-to-image', group_count: 1 },
  ])
  assert.deepEqual(modelGroupPlan({ 'gpt-image-2.5-sunburst': 9 }), [
    { image_model: 'gpt-image-2.5-sunburst', group_count: 6 },
  ])
  assert.deepEqual(modelGroupPlan({ 'gpt-image-2.5-sunburst': 0 }), [])

  const payload = buildSeriesRunPayload({
    submitId: 'submit-12345678',
    prompt: '每组 4 张',
    promptOptimized: false,
    ratio: '3:4',
    products: ['asset-1', 'asset-2'],
    brandId: 'brand',
    brandMode: 'preset',
    logoAssetId: 'logo-1',
    logoPosition: 'bottom-right',
    styleReferenceIds: [],
    modelCounts: { 'gpt-image-2.5-sunburst': 2, 'seedream-5.5-image-to-image': 1 },
    title: '系列测试',
    platformId: null,
    placementId: null,
    includeStoreAddress: false,
    includeStorePhone: false,
    storePlaceId: null,
  })
  assert.equal('pages_per_group' in payload, false)
  assert.equal(payload.prompt, '每组 4 张')
  assert.equal(payload.logo_position, 'bottom-right')
  // New series rounds always build all their pages: there is no operator toggle.
  assert.equal('auto_generate_interiors' in payload, false)
  assert.equal(payload.ratio, '3:4')
  assert.deepEqual(payload.reference_asset_ids, ['asset-1', 'asset-2'])
  assert.equal(payload.image_groups.length, 2)

  // A platform preset owns the canvas, so the ratio field is omitted like a single poster.
  const platformPayload = buildSeriesRunPayload({
    submitId: 'submit-12345678', prompt: '做 2 张', promptOptimized: true, products: ['asset-1'],
    brandId: null, brandMode: 'none', logoAssetId: null, logoPosition: 'top-left', styleReferenceIds: [],
    modelCounts: { 'gpt-image-2.5-sunburst': 1 },
    platformId: 'instagram', placementId: 'feed', includeStoreAddress: false, includeStorePhone: false,
    storePlaceId: null,
  })
  assert.equal('ratio' in platformPayload, false)
  assert.equal('logo_position' in platformPayload, false)
  assert.equal('auto_generate_interiors' in platformPayload, false)
  // The page count lives in the requirement only: no helper renders a page-count sentence.
  assert.equal('pageCountHint' in seriesModule, false)
  assert.equal('SERIES_DEFAULT_PAGES' in seriesModule, false)
  assert.equal(JSON.stringify(payload).includes('页数按设计需求'), false)
})

test('only a fully succeeded group offers the ZIP and a page downloads on its own', () => {
  const done = page({ page_id: 'p', order: 1, status: 'succeeded', asset_id: 'a', download_url: '/download/p' })
  const failed = page({ page_id: 'f', order: 2, status: 'failed' })
  assert.equal(groupDownloadReady(group({ id: 'g', order: 0, can_download: false, pages: [done, failed] })), false)
  assert.equal(groupDownloadReady(group({ id: 'g', order: 0, can_download: true, complete: true, pages: [done] })), true)
  assert.equal(pageDownloadHref(done), '/download/p')
  assert.equal(pageDownloadHref(failed), null)
  assert.equal(groupProgressLabel(group({ id: 'g', order: 0, page_count: 4, succeeded_count: 3 })), '3/4 页')
})

test('a running series keeps polling until every page settles', () => {
  const running = workspace([group({ id: 'g', order: 0,
    pages: [page({ page_id: 'c', order: 0, status: 'running' })] })])
  const settled = workspace([group({ id: 'g', order: 0,
    pages: [page({ page_id: 'c', order: 0, status: 'succeeded' })] })])
  assert.equal(seriesNeedsPoll(running), true)
  assert.equal(seriesNeedsPoll(settled), false)
  assert.equal(seriesNeedsPoll(null), false)
})

test('replacement defaults come from the current version, and the pool offers other photos', () => {
  const cover = page({ page_id: 'c', order: 0, content_refs: [{ asset_id: 'asset-1', target: '封面主图' }] })
  const detail = page({
    page_id: 'd', order: 1,
    content_refs: [{ asset_id: 'asset-2', target: '莓果杯' }],
    style_reference_ids: ['asset-6'],
    cover_reference: { version_id: 'cover-v2', asset_id: 'asset-1' },
  })
  assert.deepEqual(replacementDefaults(detail), [{ asset_id: 'asset-2', target: '莓果杯' }])
  assert.deepEqual(replacementDefaults(null), [])
  // Style references and the group cover never show up as replacement candidates.
  const pool = replacementPool(detail, ['asset-1', 'asset-2', 'asset-4', 'asset-6'])
  assert.deepEqual(pool, ['asset-2', 'asset-4'])
  const taskPool = taskAssetPool(workspace([
    group({ id: 'g', order: 0, pages: [cover, detail] }),
  ]), group({ id: 'g', order: 0, pages: [cover, detail] }), ['asset-4'])
  assert.deepEqual(taskPool.sort(), ['asset-1', 'asset-2', 'asset-4', 'asset-6'])
})

test('a POST response patches one group or page without refetching the snapshot', () => {
  const snapshot = workspace([
    group({ id: 'g1', order: 0, pages: [page({ page_id: 'p1', order: 0 })] }),
    group({ id: 'g2', order: 1, pages: [page({ page_id: 'p2', order: 0, group_id: 'g2' })] }),
  ])
  const patched = mergeSeriesPage(snapshot, page({ page_id: 'p1', order: 0, group_id: 'g1', status: 'running' }))
  assert.equal(patched?.groups[0].pages[0].status, 'running')
  assert.equal(patched?.groups[1].pages[0].status, 'planned')
  const withGroup = mergeSeriesGroup(patched, group({ id: 'g2', order: 1, interiors_started_at: 'now', pages: [] }))
  assert.equal(withGroup?.groups[1].interiors_started_at, 'now')
  assert.equal(mergeSeriesPage(null, page({ page_id: 'p', order: 0 })), null)
})

test('the isolated editor bridge accepts only this session, this canvas and PNG data', () => {
  const expected = { sessionId: 'session-1', width: 1080, height: 1920 }
  const png = 'data:image/png;base64,' + 'A'.repeat(120)
  assert.deepEqual(parseEditorMessage({ type: SERIES_EDITOR_READY, session_id: 'session-1' }, expected), { kind: 'ready' })
  assert.deepEqual(parseEditorMessage({ type: SERIES_EDITOR_READY, session_id: 'other' }, expected), { kind: 'ignore' })
  assert.deepEqual(parseEditorMessage({ type: 'unknown', session_id: 'session-1' }, expected), { kind: 'ignore' })
  assert.deepEqual(parseEditorMessage(null, expected), { kind: 'ignore' })
  const exported = parseEditorMessage({
    type: SERIES_EDITOR_EXPORT, session_id: 'session-1', png, width: 1080, height: 1920,
  }, expected)
  assert.equal(exported.kind, 'export')
  if (exported.kind === 'export') assert.equal(exported.base64, 'A'.repeat(120))
  const wrongSize = parseEditorMessage({
    type: SERIES_EDITOR_EXPORT, session_id: 'session-1', png, width: 1000, height: 1000,
  }, expected)
  assert.equal(wrongSize.kind, 'error')
  const notPng = parseEditorMessage({
    type: SERIES_EDITOR_EXPORT, session_id: 'session-1', png: 'data:image/jpeg;base64,AAAA', width: 1080, height: 1920,
  }, expected)
  assert.equal(notPng.kind, 'error')
  assert.equal(parseEditorMessage({ type: SERIES_EDITOR_ERROR, session_id: 'session-1', message: '导出失败' }, expected).kind, 'error')

  const html = '<html><head><title>editor</title></head><body><script>window.realTemplate=1</script></body></html>'
  const injected = injectEditorBridge(html, 'session-1')
  // The sandbox CSP and the bridge must precede the template's own scripts.
  assert.ok(injected.indexOf('Content-Security-Policy') > injected.indexOf('<head>'))
  assert.ok(injected.indexOf('Content-Security-Policy') < injected.indexOf('<title>'))
  assert.ok(injected.indexOf('poster-series-save-bridge') < injected.indexOf('window.realTemplate'))
  assert.ok(injected.includes(SERIES_EDITOR_CSP))
  assert.ok(!SERIES_EDITOR_CSP.includes('sandbox'))
  // A fragment without a head still gets the bridge in front of its scripts.
  const fragment = composeEditorDocument('<div>fragment</div><script>1</script>', 's')
  assert.ok(fragment.startsWith('<meta http-equiv="Content-Security-Policy"'))
  assert.ok(fragment.indexOf('poster-series-save-bridge') < fragment.indexOf('<div>fragment</div>'))
  // Real templates keep their export inside an IIFE, so the bridge drives the download button.
  const bridge = editorBridgeScript('session-1')
  assert.ok(bridge.includes(SERIES_EDITOR_EXPORT_REQUEST))
  assert.ok(bridge.includes('createObjectURL'))
  assert.ok(bridge.includes('download'))
  assert.ok(bridge.includes('HTMLAnchorElement'))
  assert.ok(bridge.indexOf('makeCanvas') > bridge.indexOf('exportButton'))
})

test('saving to this page binds the version, the artifact and the page content refs', () => {
  const body = htmlSaveBody({
    sessionId: 'session-1',
    sourceVersionId: 'version-1',
    expectedOutputId: 'output-1',
    artifactId: 'artifact-1',
    base64: 'AAAA',
    width: 1080,
    height: 1920,
    contentRefs: [{ asset_id: 'asset-4', target: '莓果杯' }],
  })
  assert.deepEqual(body, {
    source_version_id: 'version-1',
    expected_output_id: 'output-1',
    artifact_id: 'artifact-1',
    session_id: 'session-1',
    png_base64: 'AAAA',
    width: 1080,
    height: 1920,
    content_refs: [{ asset_id: 'asset-4', target: '莓果杯' }],
  })
  assert.match(editorConflictMessage({ status: 409, message: '' }), /新版本/)
  assert.match(editorConflictMessage({ status: 422 }), /画布/)
  assert.equal(editorConflictMessage({ message: '网络中断' }), '网络中断')
})

test('the draft remembers series mode and Logo position, and the task remembers its group and page', () => {
  const form = {
    products: [], prompt: '每组 3 张', promptOptimized: false,
    selectedPlatformId: '', selectedPlacementId: '', selectedRatio: '9:16' as const,
    selectedBrandId: '', brandMode: 'none' as const, selectedLogoId: 'logo-1', logoChoiceTouched: true,
    seriesLogoPosition: 'bottom-left' as const,
    styleReferences: [], selectedModel: 'gpt-image-2.5-sunburst' as const,
    modelCounts: { 'gpt-image-2.5-sunburst': 2 }, mode: 'series' as const,
  }
  assert.equal(savePosterSession('alice', 'shop', { kind: 'draft', workspaceId: 'w', form }), true)
  const restored = readPosterSession('alice', 'shop')
  assert.equal(restored?.kind, 'draft')
  if (restored?.kind === 'draft') {
    assert.equal(restored.form.mode, 'series')
    assert.equal(restored.form.seriesLogoPosition, 'bottom-left')
    // The auto-interior toggle is gone, so the draft has nothing to remember for it.
    assert.equal('autoGenerateInteriors' in restored.form, false)
  }

  const task = {
    kind: 'task' as const, workspaceId: 'w', taskId: 'page-task', selectedModel: 'gpt-image-2.5-sunburst' as const,
    mode: 'series' as const, seriesGroupId: 'group-b', seriesPageId: 'page-2',
  }
  assert.equal(savePosterSession('alice', 'shop', task), true)
  assert.deepEqual(readPosterSession('alice', 'shop'), task)

  // Single-poster sessions written before series existed still open unchanged.
  const legacy = { kind: 'task' as const, workspaceId: 'w', taskId: 'task-1', selectedModel: 'gpt-image-2.5-sunburst' as const }
  assert.equal(savePosterSession('alice', 'shop', legacy), true)
  assert.deepEqual(readPosterSession('alice', 'shop'), legacy)

  // An unknown mode is not a session this workbench can restore.
  savePosterSession('alice', 'shop', task)
  const key = [...(window.sessionStorage as MemoryStorage).data.keys()][0]
  ;(window.sessionStorage as MemoryStorage).data.set(key, JSON.stringify({ ...task, mode: 'triple' }))
  assert.equal(readPosterSession('alice', 'shop'), null)
})
