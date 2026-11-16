import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
import type { ComponentType } from 'react'
import type { ViteDevServer } from 'vite'
import type { MaterialFilterScheme } from '../src/types.ts'

let server: ViteDevServer
let Browser: ComponentType<{ scheme: MaterialFilterScheme }>
let GroupTags: ComponentType<{ group: unknown }>

before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), server: { middlewareMode: true }, appType: 'custom' })
  const module = await server.ssrLoadModule('/src/components/MaterialGroupBrowser.tsx')
  Browser = module.default
  GroupTags = module.GroupTags
})
after(async () => { await server?.close() })

const scheme = {
  id: 'legendaryspice-menu', folder_id: 'html', name: '菜单命名', recursive: true,
  rules: [], output_root: '', model: 'deepseek-v4-flash', prompt_version: 'menu-naming-v4-flat-tags',
  naming_mode: 'menu_match', active_run_id: null, created_at: '',
} satisfies MaterialFilterScheme

test('group browsing replaces the removed browser switch with grouping as the only entry', () => {
  const html = renderToStaticMarkup(createElement(Browser, { scheme }))
  assert.match(html, /照片分组/)
  assert.match(html, /开始分组/)
  assert.doesNotMatch(html, /逐张结果/)
  assert.doesNotMatch(html, /按相似组浏览/)
  assert.doesNotMatch(html, /开始 AI 识别/)
})

test('group browsing shows the verdict filter tabs and hides the empty unrecognized tab', () => {
  const html = renderToStaticMarkup(createElement(Browser, { scheme }))
  assert.match(html, /按筛选结果过滤照片组/)
  for (const label of ['全部', '通过', '不通过', '待复核', '请求失败', '已入选']) {
    assert.match(html, new RegExp(`${label}<span>0</span>`))
  }
  assert.doesNotMatch(html, /未识别/)
})

test('group tags come from the representative result', () => {
  const html = renderToStaticMarkup(createElement(GroupTags, {
    group: {
      id: 'group/a', scheme_id: 's', version: 'v', count: 3, reference_name: '菠菜雞絲湯',
      status: 'recognized', representative_file_ids: ['file/a'], recognized_count: 1, picked_count: 0,
      reference_stale: false, manual: false, previews: [],
      cover: { id: 'file/a', evaluation: { category: '食物', tags: ['食材', '菠菜'] } },
    },
  } as never))
  assert.match(html, /食物/)
  assert.match(html, /菠菜/)
  assert.match(html, /代表图/)
})

test('a group without a representative result shows no tag strip', () => {
  const html = renderToStaticMarkup(createElement(GroupTags, {
    group: {
      id: 'group/b', scheme_id: 's', version: 'v', count: 2, reference_name: null,
      status: 'unrecognized', representative_file_ids: ['file/b'], recognized_count: 0, picked_count: 0,
      reference_stale: false, manual: false, previews: [],
      cover: { id: 'file/b', evaluation: null },
    },
  } as never))
  assert.equal(html, '')
})
