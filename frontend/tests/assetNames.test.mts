import assert from 'node:assert/strict'
import test from 'node:test'
import { assetDisplayName, assetRenameFileName } from '../src/assetNames.ts'

test('asset labels omit image extensions while retaining meaningful dots in the name', () => {
  assert.equal(assetDisplayName('香辣鸭脖_Spicy Duck Neck.jpg'), '香辣鸭脖_Spicy Duck Neck')
  assert.equal(assetDisplayName('牛肚面.JPG'), '牛肚面')
  assert.equal(assetDisplayName('Mr. Keke v1.2.webp'), 'Mr. Keke v1.2')
  assert.equal(assetDisplayName('员工肖像.HEIC'), '员工肖像')
  assert.equal(assetDisplayName('logo'), 'logo')
  assert.equal(assetDisplayName('Mr. Keke'), 'Mr. Keke')
  assert.equal(assetDisplayName('.jpg'), '.jpg')
})

test('renaming the visible label preserves the original file format', () => {
  assert.equal(assetRenameFileName('新菜品', '旧菜品.JPG'), '新菜品.JPG')
  assert.equal(assetRenameFileName('新菜品.png', '旧菜品.jpg'), '新菜品.jpg')
  assert.equal(assetRenameFileName('品牌 Logo', 'logo'), '品牌 Logo')
})
