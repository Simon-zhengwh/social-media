import assert from 'node:assert/strict'
import test from 'node:test'
import { imageMention, parseImagePrompt, serializeImagePrompt, hydrateImagePrompt, imagePromptIssue } from '../src/imageMentions.ts'

const a = { assetId: 'asset-a', name: '海报.png', url: '/a.png' }
const b = { assetId: 'asset-b', name: '菜品.png', url: '/b.png' }

test('mentions follow their image identity when other references are reordered', () => {
  const value = `把${imageMention(a.assetId)}的卤鸡换成${imageMention(b.assetId)}`
  assert.equal(serializeImagePrompt(value, [a, b]), '把@图片1的卤鸡换成@图片2')
  assert.equal(serializeImagePrompt(value, [b, a]), '把@图片2的卤鸡换成@图片1')
})

test('removed references are invalid rather than silently rebound to another image', () => {
  const value = `使用${imageMention(a.assetId)}的排版`
  assert.match(imagePromptIssue(value, [b]) ?? '', /已移除/)
  assert.throws(() => serializeImagePrompt(value, [b]), /已移除/)
  assert.equal(imagePromptIssue(value, [a, b]), null)
})

test('saved request text restores into image-bound tokens without changing ordinary text', () => {
  const saved = '参考@图片1，替换为@图片2；这段说明提到图片1'
  const restored = hydrateImagePrompt(saved, [a, b])
  assert.equal(serializeImagePrompt(restored, [a, b]), saved)
  assert.deepEqual(parseImagePrompt(restored).filter(part => part.type === 'image').map(part => part.assetId), ['asset-a', 'asset-b'])
})

test('asset ids and user text remain literal through token parsing', () => {
  const special = { ...a, assetId: 'asset/一}two<&"' }
  const value = `<script>alert(1)</script>${imageMention(special.assetId)}\n继续`
  assert.equal(serializeImagePrompt(value, [special]), '<script>alert(1)</script>@图片1\n继续')
  assert.equal(parseImagePrompt(value).find(part => part.type === 'image')?.assetId, special.assetId)
})

test('invalid numbered mentions and excessive request length are reported', () => {
  assert.match(imagePromptIssue('@图片2', [a]) ?? '', /图片2/)
  assert.match(imagePromptIssue('a'.repeat(11), [], 10) ?? '', /10/)
  assert.equal(imagePromptIssue('a'.repeat(10), [], 10), null)
})
