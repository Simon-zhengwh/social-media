import assert from 'node:assert/strict'
import test from 'node:test'
import { MAX_RECOGNITION_BATCH, recognitionBatchSize } from '../src/materialRecognitionBatch.ts'

test('a partial batch never exceeds the typed count, the API limit or the remaining photos', () => {
  assert.equal(recognitionBatchSize('60', 260), 60)
  assert.equal(recognitionBatchSize('60', 12), 12)
  assert.equal(recognitionBatchSize('5000', 900), MAX_RECOGNITION_BATCH)
  assert.equal(MAX_RECOGNITION_BATCH, 200)
})

test('all unsettled photos ignore the typed count', () => {
  assert.equal(recognitionBatchSize('60', 260, true), 260)
  assert.equal(recognitionBatchSize('bad', 7, true), 7)
})

test('an empty list or an unusable count yields no request', () => {
  assert.equal(recognitionBatchSize('60', 0), null)
  assert.equal(recognitionBatchSize('', 10), null)
  assert.equal(recognitionBatchSize('0', 10), null)
  assert.equal(recognitionBatchSize('-4', 10), null)
  assert.equal(recognitionBatchSize('很多', 10), null)
  assert.equal(recognitionBatchSize('5', Number.NaN), null)
})
