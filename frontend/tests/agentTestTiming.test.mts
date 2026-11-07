import assert from 'node:assert/strict'
import test from 'node:test'
import { duration } from '../src/components/agentTestTiming.ts'

const now = Date.parse('2026-11-04T11:55:00Z')

test('sleep and late local synchronization do not extend a completed remote run', () => {
  assert.equal(duration({ started_at: '2026-11-04T11:19:17Z', finished_at: '2026-11-04T11:54:48Z',
    remote_started_at: '2026-11-04T11:19:18.806Z', remote_completed_at: '2026-11-04T11:24:31.679Z' }, now), '5分12秒')
})

test('a remote completion freezes the clock before local completion is persisted', () => {
  const run = { remote_started_at: '2026-11-04T11:19:18Z', remote_completed_at: '2026-11-04T11:23:17Z' }
  assert.equal(duration(run, now), '3分59秒')
  assert.equal(duration(run, now + 3600000), '3分59秒')
})

test('remote runs in progress use the current time', () => {
  assert.equal(duration({ remote_started_at: '2026-11-04T11:54:30Z' }, now), '0分30秒')
})

test('preparation or failures before remote start retain local timing', () => {
  assert.equal(duration({ started_at: '2026-11-04T11:53:00Z', finished_at: '2026-11-04T11:54:10Z' }, now), '1分10秒')
  assert.equal(duration({ started_at: '2026-11-04T11:54:30Z' }, now), '0分30秒')
})

test('queued and invalid timestamps do not display NaN', () => {
  assert.equal(duration({}, now), '等待开始')
  assert.equal(duration({ started_at: 'invalid' }, now), '等待开始')
  assert.equal(duration({ started_at: '2026-11-04T11:54:30Z', remote_started_at: 'invalid', finished_at: 'invalid' }, now), '0分30秒')
})

test('clock skew cannot produce a negative duration', () => {
  assert.equal(duration({ remote_started_at: '2026-11-04T11:55:01Z', remote_completed_at: '2026-11-04T11:55:00Z' }, now), '0分00秒')
})
