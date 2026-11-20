/// <reference lib="dom" />
/*
  Isolated fixture for the opt-in poster-series workbench.

  Every `/api/**` call is served from this file, so the page never reaches a real backend
  or a model. The real studio component is mounted unchanged, which is what the Orca CLI
  browser test drives.
*/
import { createRoot } from 'react-dom/client'
import WholeImageStudio from '../../src/components/WholeImageStudio'
import { ImageLightboxProvider } from '../../src/components/ImageLightbox'
import type { Asset, BrandLogoImage } from '../../src/types'
import '../../src/styles.css'

const now = '2026-11-18T00:00:00Z'
const params = new URLSearchParams(location.search)
const merchant = params.get('merchant_id') ?? 'm1'
const fresh = params.get('scenario') === 'fresh'
const single = params.get('scenario') === 'single'
const api = `/api/merchants/${merchant}/whole-image`

const assets: Asset[] = ['photo1', 'photo2', 'photo3', 'photo4', 'photo5', 'logo1'].map((id) => ({
  id, original_name: `${id}.png`, url: `/fixture/${id}.svg`, thumb_url: `/fixture/${id}.svg`,
  width: 320, height: 400, mime_type: 'image/png', created_at: now,
}))
const logos: BrandLogoImage[] = assets.filter((asset) => asset.id === 'logo1').map((asset) => ({
  asset_id: asset.id, original_name: asset.original_name, url: asset.url, width: asset.width, height: asset.height,
}))

type Page = Record<string, any>
type Group = Record<string, any>

const tasks: Record<string, Record<string, any>> = {}
const versions: Record<string, Record<string, any>[]> = {}
const replacements: Record<string, Record<string, any>[]> = {}

const OUTPUT_LABEL = '海报'

function output(id: string, preview: string) {
  return {
    id, channel: 'agent' as const, role: 'agent_result', label: OUTPUT_LABEL,
    asset_id: `asset-${id}`, width: 1080, height: 1920, selectable: true,
    preview_url: preview, download_url: preview, model_input: null,
  }
}

function makeTask(options: {
  id: string
  model: string
  preview: string
  prompt: string
  contentAssetId: string
  target: string
  series: Record<string, any>
}) {
  const task = {
    id: options.id, status: 'succeeded', stage: 'done', progress: 100,
    input: {
      image_model: options.model, prompt: options.prompt, prompt_optimized: false, ratio: '3:4',
      reference_asset_ids: [options.contentAssetId], product_asset_ids: [],
      brand_id: null, brand_mode: 'none', logo_asset_id: 'logo1', style_reference_asset_ids: [],
      candidate_count: 1, candidate_batch_id: null, generation_workspace_id: 'ws1', candidate_index: 0,
      platform_id: null, placement_id: null,
    },
    series: {
      ...options.series,
      content_refs: [{ asset_id: options.contentAssetId, target: options.target }],
      style_reference_ids: [], cover_reference: null,
      logo_asset_id: 'logo1', auto_generate_interiors: false, status: 'succeeded',
    },
    canvas: { width: 1080, height: 1920 }, provider: 'core_ai',
    outputs: [output(`out-${options.id}`, options.preview)],
    current_output_id: `out-${options.id}`, current_version_id: `ver-${options.id}-2`, current_version: 2,
    requires_output_selection: false, preview_url: options.preview, download_url: options.preview,
    messages: [], edit: { status: 'idle' }, finalization: null, error: null,
    model_input: {
      endpoint: 'generate_image', model: options.model, prompt: options.prompt, parameters: {},
      images: [{ asset_id: options.contentAssetId, name: `${options.contentAssetId}.png`, role: 'reference',
        url: `/fixture/${options.contentAssetId}.svg`, preview_url: `/fixture/${options.contentAssetId}.svg` }],
    },
    created_at: now, updated_at: now,
  }
  tasks[options.id] = task
  versions[options.id] = [1, 2].map((number) => ({
    id: `ver-${options.id}-${number}`, version: number, parent_version_id: number > 1 ? `ver-${options.id}-${number - 1}` : null,
    operation: number > 1 ? 'chat_edit' : 'generate', instruction: number > 1 ? '把标题放大' : '',
    width: 1080, height: 1920, output_asset_id: `asset-out-${options.id}`,
    preview_url: options.preview, download_url: options.preview,
    outputs: [output(`out-${options.id}`, options.preview)], created_at: now,
  }))
  return task
}

function page(values: Page): Page {
  return {
    group_id: '', run_id: 'run1', order: 0, role: 'cover', status: 'planned',
    whole_task_id: null, version_id: null, version: 0, output_id: null, asset_id: null,
    preview_url: null, thumb_url: null, download_url: null, error: null,
    content_refs: [], style_reference_ids: [], cover_reference: null, logo_box: null, ...values,
  }
}

function singleCandidate(id: string, model: string, index: number, batch: string, count: number) {
  const task: Record<string, any> = makeTask({
    id, model, preview: `/fixture/${id}.svg`, prompt: '介绍 SEEJOY 的甜品',
    contentAssetId: 'photo1', target: '', series: {},
  })
  delete task.series
  Object.assign(task.input, { generation_workspace_id: 'single-ws', candidate_batch_id: batch,
    candidate_index: index, candidate_count: count })
  if (id.startsWith('single-new-')) task.created_at = '2026-11-18T01:00:00Z'
  return task
}

if (single) {
  singleCandidate('single-seed', 'seedream-5.5-image-to-image', 0, 'seed-batch', 1)
  singleCandidate('single-old', 'gpt-image-2.5-sunburst', 0, 'earlier-batch', 1)
}

const cover1 = makeTask({
  id: 'task-cover1', model: 'gpt-image-2.5-sunburst', preview: '/fixture/poster1.svg',
  prompt: '每组 3 张 第 1 页封面', contentAssetId: 'photo1', target: '封面主图',
  series: { run_id: 'run1', group_id: 'g1', page_id: 'p1', order: 0, role: 'cover', page_count: 3 },
})
cover1.outputs.unshift({ ...output('cover1-without-logo', '/fixture/poster1-base.svg'),
  label: '无 Logo 底图', selectable: false })
const detail1 = makeTask({
  id: 'task-detail1', model: 'gpt-image-2.5-sunburst', preview: '/fixture/poster2.svg',
  prompt: '每组 3 张 第 2 页介绍页', contentAssetId: 'photo2', target: '莓果杯',
  series: { run_id: 'run1', group_id: 'g1', page_id: 'p2', order: 1, role: 'detail', page_count: 3 },
})
const cover2 = makeTask({
  id: 'task-cover2', model: 'seedream-5.5-image-to-image', preview: '/fixture/poster3.svg',
  prompt: '每组 2 页封面', contentAssetId: 'photo3', target: '甜品柜',
  series: { run_id: 'run1', group_id: 'g2', page_id: 'p4', order: 0, role: 'cover', page_count: 2 },
})
const cover3 = makeTask({
  id: 'task-cover3', model: 'gpt-image-2.5-sunburst', preview: '/fixture/poster4.svg',
  prompt: '完成的组封面', contentAssetId: 'photo4', target: '彩虹蛋糕',
  series: { run_id: 'run1', group_id: 'g3', page_id: 'p6', order: 0, role: 'cover', page_count: 2 },
})
const detail3 = makeTask({
  id: 'task-detail3', model: 'gpt-image-2.5-sunburst', preview: '/fixture/poster5.svg',
  prompt: '完成的组介绍页', contentAssetId: 'photo5', target: '千层',
  series: { run_id: 'run1', group_id: 'g3', page_id: 'p7', order: 1, role: 'detail', page_count: 2 },
})

function pageFromTask(task: Record<string, any>, status = 'succeeded'): Page {
  return page({
    group_id: task.series.group_id, page_id: task.series.page_id, order: task.series.order,
    role: task.series.role, status, whole_task_id: task.id,
    version_id: task.current_version_id, version: task.current_version,
    output_id: task.current_output_id, asset_id: `asset-out-${task.id}`,
    preview_url: task.preview_url, thumb_url: task.preview_url, download_url: task.download_url,
    content_refs: task.series.content_refs,
    cover_reference: task.series.role === 'detail' ? { version_id: 'ver-task-cover1-2', asset_id: 'photo1' } : null,
  })
}

const groups: Group[] = [
  {
    id: 'g1', run_id: 'run1', workspace_id: 'ws1', order: 0, image_model: 'gpt-image-2.5-sunburst',
    page_count: 3, status: 'running', cover_page_id: 'p1', cover_ready: true,
    interiors_started_at: '2026-11-18T00:10:00Z', auto_generate_interiors: false,
    complete: false, can_download: false, succeeded_count: 2, failed_count: 1, running_count: 0,
    canvas: { width: 1080, height: 1920 }, logo_asset_id: 'logo1', logo_box: { x: 40, y: 1700, width: 160, height: 160 },
    content_pool: ['photo1', 'photo2', 'photo3', 'photo4', 'photo5'],
    pages: [
      pageFromTask(cover1),
      pageFromTask(detail1),
      page({ group_id: 'g1', page_id: 'p3', order: 2, role: 'detail', status: 'failed',
        error: '模型未返回这一页的成图', whole_task_id: 'task-failed', content_refs: [{ asset_id: 'photo3', target: '甜品柜' }] }),
    ],
  },
  {
    id: 'g2', run_id: 'run1', workspace_id: 'ws1', order: 1, image_model: 'seedream-5.5-image-to-image',
    page_count: 2, status: 'running', cover_page_id: 'p4', cover_ready: true,
    interiors_started_at: null, auto_generate_interiors: false,
    complete: false, can_download: false, succeeded_count: 1, failed_count: 0, running_count: 0,
    canvas: { width: 1080, height: 1920 }, logo_asset_id: 'logo1', logo_box: null,
    content_pool: ['photo1', 'photo2', 'photo3', 'photo4', 'photo5'],
    pages: [
      pageFromTask(cover2),
      page({ group_id: 'g2', page_id: 'p5', order: 1, role: 'detail', status: 'planned',
        content_refs: [{ asset_id: 'photo2', target: '莓果杯' }] }),
    ],
  },
  {
    id: 'g3', run_id: 'run1', workspace_id: 'ws1', order: 2, image_model: 'gpt-image-2.5-sunburst',
    page_count: 2, status: 'complete', cover_page_id: 'p6', cover_ready: true,
    interiors_started_at: '2026-11-18T00:20:00Z', auto_generate_interiors: true,
    complete: true, can_download: true, succeeded_count: 2, failed_count: 0, running_count: 0,
    canvas: { width: 1080, height: 1920 }, logo_asset_id: 'logo1', logo_box: null,
    content_pool: ['photo1', 'photo2', 'photo3', 'photo4', 'photo5'],
    pages: [pageFromTask(cover3), pageFromTask(detail3)],
  },
]

tasks['task-failed'] = {
  ...detail1, id: 'task-failed', status: 'failed', stage: 'failed', progress: 0,
  outputs: [], current_output_id: null, current_version_id: null, current_version: 0,
  preview_url: null, download_url: null, error: { code: 'agent_failed', message: '模型未返回这一页的成图' },
  series: { ...detail1.series, page_id: 'p3', order: 2, content_refs: [{ asset_id: 'photo3', target: '甜品柜' }] },
}

// The detail page carries one Agent replacement record with a real-template HTML editor.
replacements['task-detail1'] = [{
  id: 'rec1', kind: 'agent', status: 'succeeded', stage: 'done', progress: 100,
  source_version_id: 'ver-task-detail1-2', source_output_id: 'out-task-detail1', source_version: 2,
  whole_task_id: 'task-detail1', split_all_elements: false, run_id: 'run-1',
  artifacts: [{
    id: 'artifact-editor', kind: 'html', file_name: 'editor.html', content_type: 'text/html', size: 1200,
    title: '原照编辑器', preview_url: '/tests/browser/generated/editor-moved.html',
    download_url: '/tests/browser/generated/editor-moved.html',
  }],
  source: { asset_id: 'asset-out-task-detail1', preview_url: '/fixture/poster2.svg', name: '海报', width: 1080, height: 1920 },
  replacements: [{ asset_id: 'photo2', target: '莓果杯', preview_url: '/fixture/photo2.svg', name: 'photo2.png', width: 320, height: 400 }],
  output: { asset_id: 'asset-out-task-detail1', width: 1080, height: 1920, preview_url: '/fixture/poster2.svg', download_url: '/fixture/poster2.svg' },
  error: null, agent_input: null, agent_output: null, started_at: now, deadline_at: null, created_at: now, updated_at: now,
}]

const snapshot = () => ({
  workspace_id: 'ws1', mode: 'series', locked: true, series_enabled: true,
  runs: [{
    id: 'run1', workspace_id: 'ws1', round: 1, status: 'running', prompt: '每组 3 张，介绍 SEEJOY 的甜品',
    pages_per_group: 3, auto_generate_interiors: false, canvas: { width: 1080, height: 1920 }, created_at: now,
  }],
  groups: JSON.parse(JSON.stringify(groups)),
})

const probe = { saves: [] as unknown[], probe: null as unknown, retries: [] as string[], interiors: [] as string[],
  edits: [] as unknown[], runs: [] as unknown[], replacementCreates: [] as unknown[], completeRound: null as (() => void) | null }
;(window as unknown as { __seriesProbe: typeof probe }).__seriesProbe = probe

/** Fresh-draft scenario: an empty workspace that the operator turns into a series round. */
const freshWorkspaces: Record<string, Record<string, any>> = {}

function freshRound(workspaceId: string, body: Record<string, any>) {
  const createdGroups: Group[] = []
  let order = 0
  for (const item of body.image_groups ?? []) {
    for (let index = 0; index < Number(item.group_count ?? 0); index += 1) {
      const groupId = `fresh-g${order + 1}`
      createdGroups.push({
        id: groupId, run_id: 'fresh-run', workspace_id: workspaceId, order,
        image_model: item.image_model, page_count: 0, status: 'running', cover_page_id: null,
        cover_ready: false, interiors_started_at: null, auto_generate_interiors: false,
        complete: false, can_download: false, succeeded_count: 0, failed_count: 0, running_count: 0,
        canvas: { width: 1080, height: 1920 }, logo_asset_id: body.logo_asset_id ?? null, logo_box: null,
        content_pool: body.reference_asset_ids ?? [], pages: [],
      })
      order += 1
    }
  }
  const state = {
    workspace_id: workspaceId, mode: 'series', locked: true, series_enabled: true,
    runs: [{
      id: 'fresh-run', workspace_id: workspaceId, round: 1, status: 'running', prompt: body.prompt,
      pages_per_group: null, workflow: 'agent-series', auto_generate_interiors: false,
      canvas: { width: 1080, height: 1920 }, created_at: now,
    }],
    groups: createdGroups,
    tasks: [] as Record<string, any>[],
  }
  probe.completeRound = () => {
    for (const group of createdGroups) {
      const count = group.order === 0 ? 3 : 1
      group.pages = Array.from({ length: count }, (_, index) => pageFromTask(makeTask({
        id: 'fresh-image-' + group.order + '-' + index, model: group.image_model,
        preview: '/fixture/poster' + (index + 1) + '.svg', prompt: body.prompt,
        contentAssetId: 'photo1', target: '',
        series: { run_id: 'fresh-run', group_id: group.id, page_id: group.id + '-p' + index,
          order: index, role: 'image', page_count: count },
      })))
      Object.assign(group, { page_count: count, status: 'complete', complete: true, succeeded_count: count })
    }
    state.runs[0].status = 'complete'
  }
  freshWorkspaces[workspaceId] = state
  return state
}

function findPage(groupId: string, pageId: string): Page | null {
  const group = groups.find((item) => item.id === groupId)
  return group?.pages.find((item: Page) => item.page_id === pageId) ?? null
}

async function measurePng(dataUrl: string) {
  const image = new Image()
  await new Promise<void>((resolve, reject) => {
    image.onload = () => resolve()
    image.onerror = () => reject(new Error('probe image failed'))
    image.src = dataUrl
  })
  const canvas = document.createElement('canvas')
  canvas.width = image.naturalWidth
  canvas.height = image.naturalHeight
  const context = canvas.getContext('2d', { willReadFrequently: true })!
  context.drawImage(image, 0, 0)
  const data = context.getImageData(0, 0, canvas.width, canvas.height).data
  let count = 0
  let sumX = 0
  let sumY = 0
  for (let index = 0; index < data.length; index += 4) {
    // The fixture photo is pure magenta, so its centre is a numeric position.
    if (data[index] > 200 && data[index + 1] < 60 && data[index + 2] > 200) {
      const pixel = index / 4
      count += 1
      sumX += pixel % canvas.width
      sumY += Math.floor(pixel / canvas.width)
    }
  }
  return {
    width: canvas.width, height: canvas.height, magenta: count,
    centerX: count ? Math.round((sumX / count) * 10) / 10 : null,
    centerY: count ? Math.round((sumY / count) * 10) / 10 : null,
  }
}

const realFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
  const path = new URL(url, location.origin).pathname
  const method = (init?.method ?? 'GET').toUpperCase()
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
  if (path.startsWith('/fixture/')) {
    return new Response('<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1920"><rect width="1080" height="1920" fill="#e6ecdf"/></svg>',
      { headers: { 'Content-Type': 'image/svg+xml' } })
  }
  if (!path.startsWith('/api/')) return realFetch(input, init)
  if (path === '/api/whole-image/series-config' || path === `${api}/series-config`) {
    // ?series=off keeps the feature disabled, which must leave the single-poster UI intact.
    return json({ enabled: new URLSearchParams(location.search).get('series') !== 'off' })
  }
  if (path.startsWith(api)) {
    const rest = path.slice(api.length)
    if (rest === '/workspaces/ws1/costs') {
      const details = new URL(url, location.origin).searchParams.get('details') === 'true'
      return json({
        total_usd: 0.3432, total_credits: 12, call_count: 1, pending_count: 0, unreported_count: 0,
        ...(details ? { calls: [{
          id: 'paid-1', kind: 'agent', operation: 'series', target: 'Image Editor',
          image_number: null, started_at: now, status: 'completed', cost_usd: 0.3432, credits: 12,
          children: [{ id: 'child-1', kind: 'llm', target: 'DeepSeek Flash',
            started_at: now, cost_usd: 0.0032, credits: null }],
        }] } : {}),
      })
    }
    if (single && rest === '/tasks' && method === 'POST') {
      const body = JSON.parse(String(init?.body ?? '{}'))
      return json(singleCandidate(`single-new-${body.candidate_index + 1}`, body.image_model,
        body.candidate_index, body.candidate_batch_id, body.candidate_count), 202)
    }
    const genericMode = /^\/workspaces\/([^/]+)\/mode$/.exec(rest)
    if (genericMode && genericMode[1] !== 'ws1') {
      const state = freshWorkspaces[genericMode[1]]
      return json({
        workspace_id: genericMode[1], mode: state ? 'series' : 'single',
        locked: Boolean(state), series_enabled: true,
      })
    }
    const genericSeries = /^\/workspaces\/([^/]+)\/series-groups$/.exec(rest)
    if (genericSeries && genericSeries[1] !== 'ws1') {
      return json(freshWorkspaces[genericSeries[1]] ?? {
        workspace_id: genericSeries[1], mode: 'series', locked: false, series_enabled: true, runs: [], groups: [],
      })
    }
    const genericRun = /^\/workspaces\/([^/]+)\/series-runs$/.exec(rest)
    if (genericRun && method === 'POST') {
      const body = JSON.parse(String(init?.body ?? '{}'))
      probe.runs.push(body)
      if (params.has('locked_elsewhere')) {
        return json({ detail: '当前任务已锁定为另一种海报模式，请新建海报' }, 409)
      }
      return json(freshRound(genericRun[1], body), 202)
    }
    const genericTasks = /^\/workspaces\/([^/]+)\/tasks$/.exec(rest)
    if (genericTasks && genericTasks[1] !== 'ws1') {
      return json(Object.values(tasks).filter(task => task.input.generation_workspace_id === genericTasks[1]))
    }
    const genericTitle = /^\/workspaces\/([^/]+)\/title$/.exec(rest)
    if (genericTitle && genericTitle[1] !== 'ws1') {
      return json({ workspace_id: genericTitle[1], title: '系列新任务', created_at: now })
    }
    if (rest === '/workspaces/ws1/mode') {
      return json({ workspace_id: 'ws1', mode: 'series', locked: true, series_enabled: true })
    }
    if (rest === '/workspaces/ws1/series-groups') return json(snapshot())
    if (rest === '/workspaces/ws1/tasks') return json([])
    if (rest === '/workspaces/ws1/title') return json({ workspace_id: 'ws1', title: 'SEEJOY 系列测试', created_at: now })
    const versionSelect = /^\/tasks\/([^/]+)\/versions\/([^/]+)\/select$/.exec(rest)
    if (versionSelect && method === 'POST') {
      const [, taskId, versionId] = versionSelect
      const task = tasks[taskId]
      const version = versions[taskId]?.find(item => item.id === versionId)
      if (!task || !version) return json({ detail: '版本不存在' }, 404)
      const preview = version.version === 1 ? `/fixture/${taskId}-v1.svg` : version.preview_url
      Object.assign(task, { current_version: version.version, current_version_id: version.id,
        preview_url: preview, download_url: preview, outputs: [output(task.current_output_id, preview)] })
      return json(task)
    }
    const taskMatch = /^\/tasks\/([^/]+)(\/versions|\/original-replacements|\/finalization)?$/.exec(rest)
    if (taskMatch) {
      const [, taskId, suffix] = taskMatch
      if (suffix === '/versions') return json(versions[taskId] ?? [])
      if (suffix === '/original-replacements' && method === 'POST') {
        const body = JSON.parse(String(init?.body ?? '{}'))
        probe.replacementCreates.push({ task_id: taskId, ...body })
        const owner = tasks[taskId]
        return json({
          id: 'rec-new', kind: body.kind ?? 'agent', status: 'queued', stage: 'queued', progress: 0,
          whole_task_id: taskId, source_version_id: body.source_version_id,
          source_output_id: body.source_output_id, split_all_elements: Boolean(body.split_all_elements),
          source: { asset_id: 'asset-out', preview_url: owner?.preview_url ?? '/fixture/poster2.svg',
            name: '海报', width: 1080, height: 1920 },
          replacements: [], artifacts: [], output: null, error: null, candidates: [],
          created_at: now, updated_at: now,
        }, 202)
      }
      if (suffix === '/original-replacements') return json(replacements[taskId] ?? [])
      if (suffix === '/finalization') return json({ finalization: null })
      const task = tasks[taskId]
      return task ? json(task) : json({ detail: '任务不存在' }, 404)
    }
    const edit = /^\/tasks\/([^/]+)\/edits$/.exec(rest)
    if (edit && method === 'POST') {
      const task = tasks[edit[1]]
      if (!task) return json({ detail: '任务不存在' }, 404)
      // Slow edit response: the operator can open another page before it lands.
      await new Promise((resolve) => setTimeout(resolve, 1500))
      probe.edits.push(JSON.parse(String(init?.body ?? '{}')))
      const next = { ...task, edit: { status: 'running', image_model: 'gpt-image-2.5-sunburst' } }
      tasks[edit[1]] = next
      return json(next, 202)
    }
    const retry = /^\/series-groups\/([^/]+)\/pages\/([^/]+)\/retry$/.exec(rest)
    if (retry && method === 'POST') {
      probe.retries.push(`${retry[1]}:${retry[2]}`)
      const target = findPage(retry[1], retry[2])
      if (!target) return json({ detail: '页面不存在' }, 404)
      target.status = 'running'
      target.error = null
      return json(target, 202)
    }
    const save = /^\/series-groups\/([^/]+)\/pages\/([^/]+)\/html-saves$/.exec(rest)
    if (save && method === 'POST') {
      const body = JSON.parse(String(init?.body ?? '{}'))
      // Keep the request in flight so the driver can switch pages mid-save.
      await new Promise((resolve) => setTimeout(resolve, 1500))
      const measured = await measurePng(`data:image/png;base64,${body.png_base64}`)
      probe.saves.push({ ...body, measured })
      probe.probe = measured
      const target = findPage(save[1], save[2])
      if (!target) return json({ detail: '页面不存在' }, 404)
      const next = { ...target, status: 'succeeded', version: 3, version_id: 'ver-task-detail1-3' }
      return json(next)
    }
    const interiors = /^\/series-groups\/([^/]+)\/interiors$/.exec(rest)
    if (interiors && method === 'POST') {
      probe.interiors.push(interiors[1])
      const group = groups.find((item) => item.id === interiors[1])
      if (!group) return json({ detail: '组不存在' }, 404)
      group.interiors_started_at = now
      return json(group, 202)
    }
  }
  return json({ detail: `Unhandled fixture request ${method} ${path}` }, 404)
}

if (fresh) {
  // A draft the operator turns into a series round: one reference image, no group counts yet.
  const key = `social-media:poster-session:v1:${encodeURIComponent('fixture')}:${encodeURIComponent(merchant)}`
  sessionStorage.setItem(key, JSON.stringify({
    kind: 'draft', workspaceId: 'fresh-ws', title: '系列新任务',
    form: {
      products: [assets[0]], prompt: '每组 2 张，介绍我们的甜品', promptOptimized: false,
      selectedPlatformId: '', selectedPlacementId: '', selectedRatio: '9:16',
      selectedBrandId: '', brandMode: 'none', selectedLogoId: '', logoChoiceTouched: true,
      styleReferences: [], selectedModel: 'gpt-image-2.5-sunburst', modelCounts: {},
      mode: 'single',
    },
  }))
}

createRoot(document.getElementById('root')!).render(
  <ImageLightboxProvider>
    <WholeImageStudio userId="fixture" active newDesignHref="#" brandStudioHref="#"
      merchantProfile={null} brands={[]} merchantLogos={logos} brandsLoading={false} brandsError={null}
      platforms={[]} platformsLoading={false} platformsError={null} assets={assets} assetsLoading={false}
      onLoadAssets={() => {}} onGoAssets={() => {}} />
  </ImageLightboxProvider>,
)
