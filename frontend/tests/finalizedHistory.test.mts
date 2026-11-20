import assert from 'node:assert/strict'
import test from 'node:test'
import { groupFinalizedHistory, type HistoryBatchRef } from '../src/finalizedHistory.ts'
import type { PosterFinalization } from '../src/types.ts'

function makeFinalization(overrides: Partial<PosterFinalization>): PosterFinalization {
  return {
    id: 'final_1',
    group_key: 'ws1',
    task_id: 't1',
    version_id: 'v1',
    output_id: 'out1',
    version: 1,
    asset_ids: [],
    revision: 'rev1',
    finalized_at: '2026-11-20T00:00:00Z',
    preview_url: '/preview/final.png',
    thumb_url: '/thumb/final.png',
    task_url: '?whole=t1',
    ...overrides,
  }
}

test('groupFinalizedHistory resolves single poster round, model, image and version thumbnail', () => {
  const batches: HistoryBatchRef[] = [
    {
      id: 'batch-1',
      round: 1,
      model: 'GPT Image 2.5',
      images: [
        { id: 't1', taskId: 't1', label: '图片 1', thumb: '/thumb/latest-v3.png' },
        { id: 't2', taskId: 't2', label: '图片 2', thumb: '/thumb/t2.png' },
      ],
    },
    {
      id: 'batch-2',
      round: 2,
      model: 'Gemini 3.1 Flash Image',
      images: [{ id: 't3', taskId: 't3', label: '图片 1', thumb: '/thumb/t3.png' }],
    },
  ]

  const groups = groupFinalizedHistory(batches, [
    makeFinalization({
      id: 'final_t1',
      task_id: 't1',
      version_id: 't1-v2',
      version: 2,
      thumb_url: '/thumb/t1-v2.png',
    }),
  ])

  assert.equal(groups.length, 1)
  assert.equal(groups[0].round, 1)
  assert.equal(groups[0].model, 'GPT Image 2.5')
  assert.equal(groups[0].group, undefined)
  assert.deepEqual(groups[0].items.map(item => ({
    imageId: item.imageId,
    label: item.label,
    version: item.version,
    versionId: item.versionId,
    thumb: item.thumb,
  })), [{
    imageId: 't1',
    label: '图片 1',
    version: 2,
    versionId: 't1-v2',
    thumb: '/thumb/t1-v2.png',
  }])
})

test('groupFinalizedHistory groups series pages across rounds, models and groups in newest-round order', () => {
  const batches: HistoryBatchRef[] = [
    {
      id: 'g1',
      round: 7,
      model: 'Gemini 3.1 Flash Image',
      group: '组 2',
      images: [
        { id: 'p-7-1', taskId: 'st-7-1', label: '图片 1', thumb: '/thumb/7-1.png' },
      ],
    },
    {
      id: 'g2',
      round: 8,
      model: 'GPT Image 2.5',
      group: '组 1',
      images: [
        { id: 'p-8-1', taskId: 'st-8-1', label: '图片 1', thumb: '/thumb/8-1.png' },
        { id: 'p-8-2', taskId: 'st-8-2', label: '图片 2', thumb: '/thumb/8-2.png' },
      ],
    },
  ]

  const groups = groupFinalizedHistory(batches, [
    makeFinalization({ id: 'f1', group_key: 'series:g1:p-7-1', task_id: 'st-7-1', version_id: 'v3', version: 3 }),
    makeFinalization({ id: 'f2', group_key: 'series:g2:p-8-2', task_id: 'st-8-2', version_id: 'v1', version: 1 }),
    makeFinalization({ id: 'f3', group_key: 'series:g2:p-8-1', task_id: 'st-8-1', version_id: 'v2', version: 2 }),
    makeFinalization({ id: 'f4', group_key: 'series:other:p', task_id: 'unknown-task', version_id: 'v1', version: 1 }),
  ])

  assert.deepEqual(groups.map(group => ({
    round: group.round,
    model: group.model,
    group: group.group,
    items: group.items.map(item => `${item.label}:v${item.version}`),
  })), [
    { round: 8, model: 'GPT Image 2.5', group: '组 1', items: ['图片 1:v2', '图片 2:v1'] },
    { round: 7, model: 'Gemini 3.1 Flash Image', group: '组 2', items: ['图片 1:v3'] },
  ])
})
