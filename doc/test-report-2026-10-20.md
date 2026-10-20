# 2026-10-20 多轮编辑与 Logo 融合测试报告

> 测试对象：`D:\ai_project\social-media`  
> 测试时间：2026-10-20 凌晨  
> 测试方式：pytest + 真实 ToAPIs 生成 + 内置浏览器（Codex In-app Browser）E2E + 版本不变量审计  
> 结论：两项需求已实现并通过端到端验证；测试过程中发现并修复 11 项问题。当前最终交付任务 `task_7ee792ddc6fa43c0ad5152105bf24f3d` 的 `v2` 无质量告警、无未授权图层改动，并已完成广告感重排版。

---

## 1. 本次交付

### 1.1 Logo 不再由代码贴底牌

- 删除了旧代码在左上角绘制不透明圆角底牌的逻辑。
- 新的 Logo 流程：
  1. 取 Logo 预留框周围的**局部底图**；
  2. 把局部底图 + 透明 Logo 原图交给生图模型融合；
  3. 把融合后的局部贴回底图；
  4. 清除模型可能留下的多余 Logo 残影；
  5. 用**原始透明 Logo** 在预留框内等比精确覆盖。
- 效果：
  - 不再出现白色/奶油色卡片；
  - 保留模型带来的环境光影；
  - 品牌字形、颜色、透明边缘来自原文件，不会被模型改写；
  - Logo 原始宽高比由代码强制保持；
  - 局部融合失败时自动退化为透明叠加，不会退回底牌方案。

### 1.2 多轮编辑与图层

- 设计状态拆为：
  - `background`：干净底图 + 当前渲染底图；
  - `text`：主标题、副标题、价格、促销、活动信息、CTA、品牌名、补充说明；
  - `logo`：原始品牌素材引用 + 位置/尺寸/透明度/融合模式。
- 支持：
  - 对话改文案、改价格；
  - 改字号、颜色、对齐、字重；
  - 移动/缩放 Logo；
  - 重新生成背景；
  - 显示/隐藏图层；
  - 直接在图层面板内联改字；
  - 版本列表、版本预览、恢复历史版本；
  - 下载当前版本或指定历史版本。
- 关键约束：
  - 改文字只更新目标文字层；
  - 改文字不重新生成底图；
  - 改背景只更新底图，文字层和 Logo 层原样保留；
  - 每个版本使用独立输出对象键，不覆盖历史成品。

---

## 2. 测试环境

| 项 | 值 |
|---|---|
| 操作系统 | Windows |
| Python | 3.12（`backend/.venv`） |
| 前端 | React 19 + TypeScript + Vite 8 |
| 数据库 | MongoDB 8.0，Docker，`127.0.0.1:27018` |
| 对象存储 | 腾讯云 COS `simon-1349795454` / `ap-guangzhou` |
| 生图模型 | `gpt-image-2.5-flare` |
| 设计/排版模型 | `gpt-6-sol-official`（支持视觉输入） |
| 编辑路由模型 | `deepseek-v4-flash`（复杂指令兜底） |
| 浏览器 | Codex In-app Browser |

说明：`gpt-5.4` 在当前 ToAPIs 账号返回 `model_not_found`，已切换到可用的 `gpt-6-sol-official` 并验证视觉输入。

---

## 3. 自动化测试

### 3.1 后端

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest
```

**结果：70 passed**

覆盖文件：

| 文件 | 覆盖内容 |
|---|---|
| `tests/test_design.py` | 图层结构、文案/样式/Logo/背景动作、角色匹配、精确文案、未授权字段拒绝 |
| `tests/test_designer.py` | 底图提示词、Fallback 排版、Logo 与文字避让、Logo 宽高比 |
| `tests/test_renderer.py` | 输出尺寸、透明 Logo、无底板 |
| `tests/test_text_layout.py` | 中文换行、自动缩放、显式换行、对齐、z-index、输入不可变 |
| `tests/test_quality.py` | 文字对比度、描边对比度、溢出提示 |
| `tests/test_pipeline.py` | 首次生成、失败、恢复、多图、Logo 精确覆盖、中断标记 |
| `tests/test_toapis.py` | 上传/提交/轮询/下载、错误分支、下载重试 |
| `tests/test_assets.py` | 上传、格式、大小、路径安全 |

重点新增用例：

- 改主标题后，底图、Logo、价格层逐字段相等；
- 改样式不触碰底图和 Logo；
- 移动 Logo 不触碰文字；
- 换背景只换底图，文字和 Logo 层完全相等；
- 隐藏已融合 Logo 时自动切回干净底图，避免残影；
- 删除 Logo 时同步移除底图中的 Logo；
- `副标题` 不会误匹配 `主标题`，`活动信息` 不会误匹配 `促销`；
- `价格改成深色` 被识别为样式修改，而不是把“深色”写成文案；
- `价格改成 169 元` 的新文案严格等于用户原文，模型不能追加 `/位`；
- 下载流中断时自动重试 3 次，仍失败返回中文提示。

### 3.2 前端

```powershell
Set-Location frontend
npm run build
npm run lint
```

**结果：**

- `npm run build` 通过（tsc + Vite）；
- `npm run lint` 通过，无 warning。

---

## 4. 真实生成测试

### 4.1 任务 A：9:16 连续编辑（旧代码 + 逐步修复）

- 任务：`task_81f0ba91a9524eb4bdeb305ec8f16c18`
- 素材：`seejoy火锅.png` + `seejoy_logo.png`
- 画布：1080 × 1920
- 版本数：18
- 实际执行：
  - 首次生成；
  - 改主标题；
  - 改价格（169 元、99 元）；
  - 标题放大；
  - 直接编辑主标题；
  - Logo 移动/放大；
  - 背景换夜晚餐厅；
  - 隐藏/显示 Logo；
  - 恢复 v1、v9 等历史版本。

不变量审计：

```powershell
Set-Location backend
.\.venv\Scripts\python.exe scripts\verify_multiturn.py --task-id task_81f0ba91a9524eb4bdeb305ec8f16c18
```

结果：`18 versions, 1 violation, assets_ok=true`。

唯一违规是 **v1 → v2**：旧实现让编辑模型在改文案时顺手把标题字号从 164 缩到 120。该问题已通过“显式改文案优先走规则解析”修复；v3 之后没有未授权改动。

### 4.2 任务 B：4:5 新代码完整回归

- 任务：`task_de347f11193145038ff052dde8f2696f`
- 素材：`seejoy火锅.png` + `seejoy_logo.png`
- 画布：1080 × 1350
- 版本数：9
- 实际执行：
  - 首次生成；
  - Logo 放大；
  - 主标题改回相同文案（no-op）；
  - 背景换更亮午市餐厅；
  - 历史版本恢复；
  - 品牌名/副标题/活动信息改深色。

不变量审计：

```powershell
.\.venv\Scripts\python.exe scripts\verify_multiturn.py --task-id task_de347f11193145038ff052dde8f2696f --output ..\storage\verification\multiturn-invariants-new.json
```

结果：`9 versions, 1 violation, assets_ok=true`。

唯一违规是 **v6 → v7**：角色匹配使用简单子串，`副标题` 里的“标题”误命中了主标题，`活动信息` 里的“活动”误命中了促销层，导致 v7 同时改了主标题和促销层。该问题已通过角色专属正则 + 优先级修复；v8/v9 正确只改品牌名、副标题、活动信息，最终 `v9` 质量告警为 0。

审计文件：

- `storage/verification/multiturn-invariants.json`（任务 A）
- `storage/verification/multiturn-invariants-new.json`（任务 B）

### 4.3 任务 C：广告感重排版（最终交付）

- 任务：`task_7ee792ddc6fa43c0ad5152105bf24f3d`
- 首版排版发现严重问题：7 个文字层全部挤在左上角，最高只到画布 35% 高度，下面大片空白，视觉上像 PPT。
- 修复：
  - 新增 `repair_layout`：检测文字堆叠、价格位置过高、纵向利用率不足，自动切换到广告构图；
  - 新增 `promo_layout`：超大标题在顶部、价格色带在底部、促销倾斜徽章、CTA 小标签、活动信息底部、Logo 右上；
  - 文字层支持旋转，价格/促销/CTA 可带旋转色块；
  - 新增 `relayout` 动作，用户可以说“重新排版，广告感强一点”，不重新生图即可重排。
- 任务 C 首版 → 执行“重新排版，广告感强一点” → `v2`：
  - 标题字号约 156px，价格约 108px，促销约 36px，活动信息约 22px，层级差异明显；
  - 价格红色色带、促销红色徽章、CTA 奶油色标签均带轻微旋转；
  - 文字内容、底图资产、Logo 层完全不变；
  - 质量告警 0；
  - 视觉上已明显接近用户提供的 SeeJoy 参考风格，不再是左上角文字列表。

---

## 5. 内置浏览器 E2E

测试地址：`http://127.0.0.1:5173/?task=...`

已验证：

1. 任务恢复：刷新页面后任务、预览、消息、图层、版本全部恢复；
2. 对话修改：
   - 点击“价格改成 169 元”快捷指令；
   - 轮询期间显示“识别修改/合成海报”；
   - 完成后预览变为 `169 元`；
   - 消息只出现一个“更新文案”动作 chip；
   - 底图资产 ID 不变。
3. 图层面板：
   - 9 个图层按顺序显示；
   - 底图标记“锁定”；
   - 选中主标题后出现边界框、字号/颜色/对齐控件；
   - 内联改字 `周末双人套餐 限时开吃` 成功，版本递增，字号未被意外修改。
4. 版本面板：
   - 版本列表、当前版本高亮、缩略图加载；
   - 点击“恢复到此版本”后生成新版本并刷新预览；
   - v1 恢复后主标题/价格/字号回到初稿。
5. 质量提示：
   - 换亮色背景后，界面显示“品牌名/副标题/活动信息对比度偏低”；
   - 通过对话“把品牌名、副标题和活动信息改成深色”后，告警消失；
   - 主标题和促销层颜色保持不变。
6. 下载：
   - 点击“下载 PNG”成功；
   - 文件为 `1080 × 1350` RGB PNG，约 2.49 MB。
7. 资产管理页：
   - 4 个素材正常显示；
   - 上传、用于海报、删除按钮可用；
   - 没有因为工作台改造而回归。

界面截图：

- `storage/verification/ui-studio.png`：对话 + 预览 + 底部状态；
- `storage/verification/ui-quality-warning.png`：质量提示 + 失败/重试消息 + 对话；
- `storage/verification/ui-layers.png`：图层面板 + 选中主标题 + 内联编辑控件；
- `storage/verification/ui-versions.png`：版本列表 + 缩略图；
- `storage/verification/ui-final-chat.png`：最终 v9 对话记录（v7 误改与 v9 修正对比）。
- `storage/verification/ui-ad-final.png`：最终任务 C 的广告感重排版预览。

海报截图：

- `storage/verification/multiturn-v1.png`：旧 Logo 底牌版本（问题样本）；
- `storage/verification/multiturn-v8.png`：模型融合后 Logo 被压扁/位移（问题样本）；
- `storage/verification/multiturn-v9-bg.png`：夜间背景替换，文字层保留；
- `storage/verification/multiturn-v1-4x5.png`：模型融合 + 精确覆盖后出现重影（问题样本）；
- `storage/verification/multiturn-v2-4x5.png`：去除重影后的 4:5 成品；
- `storage/verification/multiturn-v4-4x5-day.png`：亮色背景导致文字对比度不足（质量告警样本）；
- `storage/verification/multiturn-final-v9.png`：最终成品（Logo 精确、无底牌、文字可读、质量告警 0）。
- `storage/verification/final-task-c-ad-v2.png`：最终交付成品（广告感重排版、价格色带、倾斜徽章）。

---

## 6. 发现并修复的问题

| # | 严重级别 | 问题 | 修复 |
|---|---|---|---|
| 1 | P0 | Logo 被贴在不透明圆角底牌上，显得呆板 | 删除底牌代码；改为透明叠加/模型局部融合 |
| 2 | P0 | 模型把 Logo 放到左上角，压住主标题 | 局部裁切融合 + 位置精确回贴 + 文字/Logo 避让 |
| 3 | P1 | Logo 框为 155×100，方形 Logo 被压扁、文字被裁切 | `fit_logo_box` 强制原始宽高比 |
| 4 | P1 | 模型融合 + 原始覆盖产生双层 Logo 重影 | 在预留框周围恢复干净底图，再覆盖原始 Logo |
| 5 | P0 | 编辑模型把“169 元”改写成“169元/位” | 显式文案优先规则解析；模型改写文案直接拒绝 |
| 6 | P0 | `副标题` 误匹配 `主标题`、`活动信息` 误匹配 `促销` | 角色专属正则 + 优先级 + 负向断言 |
| 7 | P1 | 换亮背景后文字对比度不足，但界面没有提示 | 新增渲染后可读性检查；质量告警 + 深色文字快捷修复 |
| 8 | P1 | ToAPIs 下载流中断导致编辑失败，错误信息是英文原始异常 | 下载重试 3 次；失败信息改为中文；失败时保留上一版成品 |
| 9 | P2 | 动作 chip 重复显示整次修改摘要 | 按动作类型输出“更新文案/移动图层/缩放图层” |
| 10 | P2 | 版本缩略图首次显示空白 | 实际是懒加载，约 2 秒后加载；已确认非数据丢失 |
| 11 | P1 | 排版模型把文字全部堆在左上角，像 PPT | 新增 `repair_layout` / `promo_layout`，自动撑满画布；新增“重新排版”动作和文字旋转 |

---

## 7. 图片质量结论

### 最终成品 `final-task-c-ad-v2.png`

- 画布：1080 × 1350；
- 标题：`夜宵时间到`，约 156px、900 字重、红描边 + 阴影、轻微旋转；
- 副标题：`火锅 · 寿司 · 甜品 · 更多`，黄色小字；
- 促销：`儿童5折｜5岁以下免费`，红色倾斜徽章；
- 价格：`$56.99/位`，底部红色色带 + 黄色大字；
- CTA：`欢迎到店`，奶油色倾斜小标签；
- 活动信息：营业时间 + 地址，底部小字；
- Logo：原始 SeeJoy 圆形标，右上角，无底牌、无重影、比例正确；
- 质量告警：0；
- 可读性：良好；标题/价格/促销/CTA 字号差异明显，接近用户提供的 SeeJoy 参考风格，不再像 PPT。

### 仍需注意

- 亮色背景替换后，原文字样式可能对比度不足；系统会提示，但不会自动改用户未授权的文字样式。
- 模型局部融合仍可能改变 Logo 周围少量光影；精确覆盖保证品牌字形，但极端背景下边缘可能需要人工微调。
- 画布目前不支持鼠标拖拽，位置调整通过图层四角按钮或对话完成。
- 首次生成包含两次生图（底图 + Logo 局部融合），耗时和成本高于旧版；Logo 位置/尺寸修改会再次触发局部融合。
- 版本历史中保留了调试阶段的中间版本（v2 旧实现、v7 角色误匹配）。它们可作为问题复现证据；最终交付版本是 v9。

---

## 8. 复现命令

```powershell
# 后端
Set-Location D:\ai_project\social-media\backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1

# 前端
Set-Location D:\ai_project\social-media\frontend
npm run dev -- --host 127.0.0.1

# 自动化测试
Set-Location D:\ai_project\social-media\backend
.\.venv\Scripts\python.exe -m pytest

# 不变量审计
.\.venv\Scripts\python.exe scripts\verify_multiturn.py --task-id <task_id>
```

打开：

- 工作台：`http://127.0.0.1:5173/?task=task_de347f11193145038ff052dde8f2696f`
- API 文档：`http://127.0.0.1:8000/docs`

---

## 9. 执行说明

本次尝试派发 3 个子代理（图片审计、渲染引擎、前端工作台），但当前运行时只把环境上下文传给子代理，任务正文没有送达，子代理返回“没有收到任务”。为避免阻塞交付，最终由主代理完成实现与测试，并使用独立审计脚本、真实 ToAPIs 调用和内置浏览器完成交叉验证。
