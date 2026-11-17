import assert from 'node:assert/strict'
import test from 'node:test'
import {
  hydrateImagePrompt,
  imageLabel,
  imageMention,
  imagePromptIssue,
  serializeImagePrompt,
  type PromptImage,
} from '../src/imageMentions.ts'

const poster: PromptImage = { assetId: 'poster', name: '当前海报', url: '/poster.png', removable: false, role: 'poster' }
const first: PromptImage = { assetId: 'a', name: '图一', url: '/a.png' }
const second: PromptImage = { assetId: 'b', name: '图二', url: '/b.png' }
const editImages = [poster, first, second]
const generationImages = [first, second]

test('the poster is @海报 and references start at 图1', () => {
  assert.equal(imageLabel(editImages, 0), '海报')
  assert.equal(imageLabel(editImages, 1), '图1')
  assert.equal(imageLabel(editImages, 2), '图2')
  // Without a poster the generation prompt keeps its positional numbering.
  assert.equal(imageLabel(generationImages, 0), '图片1')
  assert.equal(imageLabel(generationImages, 1), '图片2')
})

test('a sent instruction names the poster and references, not payload positions', () => {
  const draft = `把 ${imageMention('poster')} 的背景换成 ${imageMention('b')} 里的场景`
  assert.equal(serializeImagePrompt(draft, editImages), '把 @海报 的背景换成 @图2 里的场景')
  assert.equal(imagePromptIssue(draft, editImages, 1000), null)
})

test('an unknown reference number is refused', () => {
  assert.equal(imagePromptIssue('参考 @图3 调整', editImages, 1000), '引用的图3不存在，请重新选择。')
  assert.equal(imagePromptIssue('参考 @图1 调整', [poster], 1000), '引用的图1不存在，请重新选择。')
})

test('a removed reference is reported instead of silently dropped', () => {
  const draft = `参考 ${imageMention('a')} 调整`
  assert.equal(imagePromptIssue(draft, [poster, second], 1000), '引用的图片已移除，请删除该引用或重新添加图片。')
})

test('generation prompts keep the legacy @图片N validation', () => {
  assert.equal(imagePromptIssue('把 @图片2 换掉', generationImages, 1000), null)
  assert.equal(imagePromptIssue('把 @图片3 换掉', generationImages, 1000), '引用的图片3不存在，请重新选择。')
})

test('hydrating a draft restores tokens for both label schemes', () => {
  assert.equal(hydrateImagePrompt('把 @海报 的背景换成 @图2', editImages),
    `把 ${imageMention('poster')} 的背景换成 ${imageMention('b')}`)
  assert.equal(hydrateImagePrompt('用 @图片1 的风格', generationImages), `用 ${imageMention('a')} 的风格`)
})
