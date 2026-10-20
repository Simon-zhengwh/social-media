# Reference-Guided Layer Generation 测试报告

日期：2026-10-20  
测试环境：Windows + Codex In-app Browser + FastAPI + React/Vite + MongoDB + 腾讯云 COS + core-ai  
最终任务：`task_801f8f73a45c44fdaf039170ba4b0448`  
最终版本：`v9`（内容恢复到 `v4` 的主标题笔刷版本）

## 1. 结论

**结论：MVP 通过。**

- 已从“单次底图 + Pillow 文字/矩形”升级为“全局参考稿 → 干净底图 → 三类视觉组件 → 结构化合成 → 视觉审查 → 组件级重生成”的可运行链路。
- 主标题、价格、优惠贴片均成为 `visual_component`，生成素材带透明通道，业务文字仍是可编辑 text child。
- 最终图不再呈现“价格红矩形 + 普通文字”的 UI 拼装感；标题笔刷、价格丝带、优惠贴片与整体深绿/品牌红/奶油白语言一致。
- 浏览器内已验证：生成、改字、拖动、缩放、组件级重生成、版本恢复、刷新恢复、PNG 下载。
- 组件级重生成只替换该组件素材，其他图层与文字内容保持不变。

仍需继续优化：

- 优惠贴片与右侧菜品/奶油纸边的局部关系仍有一处视觉审查提示。
- 标题与右上 Logo 的安全间距偏紧，后续可由布局模型继续校准。
- 当前画布交互是 DOM 图层框 + 服务端权威渲染；Fabric.js/Konva.js 与浏览器端导出尚未迁移。

## 2. 架构实现

### 2.1 调用顺序

```text
Design Director
  → DesignIntent / copy_items / component responsibilities
Global Design Reference
  → 完整海报参考稿（不作为最终成图）
Clean Photographic Plate
  → 去除参考稿中的文字与排版，保留摄影构图
Layout Planner
  → 同时查看干净底图 + 全局参考稿，校准文字/组件位置
Component Generation
  → title brush / price ribbon / offer sticker（统一引用同一份全局参考）
Pillow Composition
  → 品红抠图、透明通道、裁切、缩放、Scene v3 合成
Visual Critic
  → 审查拼贴感、层级、图文融合与接缝
Targeted Regeneration
  → 仅重生成指定 visual_component 的素材
```

### 2.2 组件策略

| 组件 | 生成方式 | 文字编辑 | 重生成 |
|---|---|---|---|
| 主标题 | AI 生成绿色/红色干刷笔触底板 | 原生 text child，可改字 | 只换笔刷素材 |
| 价格 | AI 生成不规则红色笔刷丝带 | `$56.99/位` 原生文字 | 只换丝带素材 |
| 优惠贴片 | AI 生成绿色云朵/花瓣贴片 | `儿童5折` 原生文字 | 只换贴片素材 |
| 背景 | 由全局参考稿转为无文字摄影底图 | 不适用 | 背景重生成仍走独立流程 |
| Logo | 原始透明素材独立覆盖 | 不重绘 | 不调用图片模型 |

### 2.3 代码位置

- `backend/app/services/reference_guided.py`：全局参考、干净底图、组件 prompt、品红抠图、缓存、组件分组。
- `backend/app/services/pipeline.py`：参考驱动主流程、组件合成、组件级重生成。
- `backend/app/services/design.py`：`visual_component` 内容编辑与 text child 同步。
- `backend/app/services/designer.py`：布局阶段接收全局参考稿。
- `backend/app/routes/tasks.py`：任务输出增加参考/底图 URL，新增组件重生成端点。
- `frontend/src/components/ResultPanel.tsx`：图层选择、拖动、右下角缩放。
- `frontend/src/components/LayerPanel.tsx`：组件文字编辑、组件缩放、单组件重生成按钮。
- `frontend/src/components/GenerateForm.tsx`：参考驱动分层 / 经典分层切换。

## 3. 自动化测试

| 命令 | 结果 |
|---|---|
| `backend/.venv/Scripts/python.exe -m pytest` | **109 passed** |
| `npm run build` | 通过 |
| `npm run lint` | 通过，无 warning/error |

新增测试覆盖：

- 品红底笔刷素材可正确抠成 RGBA，角落透明、主体保留。
- 参考驱动任务生成 `headline`、`promotion_price`、`offer_badge` 三类组件。
- 每个组件有独立 `component` 素材引用。
- 组件级重生成只改变素材 asset_id，不改变原生文字内容。
- 组件重生成会新增版本并保留其他图层。

## 4. 真实模型调用

本次真实链路共产生：

- 图片调用 **6 次**：全局参考 1、干净底图 1、标题 1、价格 1、优惠 1、组件重生成 1。
- 对话调用 **3 次**：设计规划 1、布局校准 1、视觉审查 1。
- 文本修改、拖动、缩放、版本恢复、刷新恢复、PNG 下载：**0 次图片调用**。

关键时间：

- 首次完整生成：约 **6 分 53 秒**。
- 单组件重生成：约 **35 秒**。
- 最终 PNG 导出：1080 × 1920、RGB、2,737,918 bytes，sha256 `edf1acfbff16be37415f2942f643ba7ed9f1cdf8477ea9c89add3e094ccb8b2f`。
- `v9` 与 `v4` 成品 sha256 完全一致，证明版本恢复没有改变最终像素。

## 5. 内置浏览器 E2E

### 5.1 生成

1. 打开 `http://127.0.0.1:5173`。
2. 选择 `seejoy火锅.png`，保留自动匹配的 SeeJoy Logo。
3. 选择 `9:16` 和「参考驱动分层」。
4. 输入用户原文：`周末相聚`、`$56.99/位`、`儿童5折`。
5. 点击生成，阶段依次出现：
   - `理解需求与文案`
   - `生成全局设计参考`
   - `生成场景底图`
   - `规划对象与文字`
   - `生成标题/价格/贴片素材`
   - `视觉审查成图`
   - `已完成`
6. 结果元数据：
   - `1080 × 1920`
   - `v1`
   - `3 个视觉组件`
   - `4 个文字层`
   - `11 个形状层`
   - `Logo 透明层`

### 5.2 改字

1. 图层面板选择「主标题视觉组件」。
2. 点击「编辑组件文字」。
3. 将 `周末相聚` 改为 `周末欢聚` 并保存。
4. 页面进入 `v2`，标题立即更新，底图、价格、优惠、Logo 不变。
5. 再改回 `周末相聚`，进入 `v3`。
6. 后端日志中这两个 PATCH 请求之后没有 `/images/generations` 或 `/images/edits` 请求。

### 5.3 组件级重生成

1. 选中主标题组件。
2. 点击「只重生成这个组件的笔刷素材」。
3. 页面显示 `只重生成当前组件`。
4. 约 35 秒后进入 `v4`。
5. 只有标题笔刷素材 asset_id 变化；文字仍为 `周末相聚`；价格、优惠、背景、Logo 不变。
6. 新素材仍是绿色/红色干刷笔触，透明边缘正常。

### 5.4 拖动与缩放

- 拖动「手写批注 2」后进入 `v5`，说明 canvas 图层框可拖拽并保存位置。
- 选中主标题组件点击「放大组件」，宽度从 `1034` 变为 `1137`，进入 `v8`，说明组件缩放有效。
- 两次操作均未调用图片模型。
- 最终通过版本恢复回到 `v4` 对应内容，保存为 `v9`。

### 5.5 版本与刷新

- 版本面板能够列出 `v1`–`v9`。
- 刷新页面后仍能从 `?task=task_801f8f73a45c44fdaf039170ba4b0448` 恢复任务、预览和版本号。
- 恢复 `v4` 后，主标题笔刷、价格丝带、优惠贴片和所有文字均一致。

### 5.6 下载

浏览器实际下载：

`C:\Users\simonzheng\Downloads\poster-task_801f8f73a45c44fdaf039170ba4b0448.png`

PIL 验证：`PNG 1080 × 1920 RGB`，可正常解码。

### 5.7 控制台

浏览器 error/warning 日志：**0 条**。

## 6. 视觉结果

### 6.1 参考稿 vs 最终图

- 参考稿：`storage/deliverables/design-reference-thumb.jpg`
- 最终图：`storage/deliverables/final-thumb.jpg`
- 干净底图：`storage/deliverables/clean-background-thumb.jpg`
- 组件素材：`storage/deliverables/components-thumb.jpg`

透明素材指标：

| 组件 | 尺寸 | 透明抠图后主体覆盖率 |
|---|---:|---:|
| 主标题笔刷 | 996 × 501 | 56.60% |
| 价格丝带 | 969 × 432 | 53.02% |
| 优惠贴片 | 859 × 580 | 58.77% |

### 6.2 判断

**通过项：**

- 主标题不再是 Pillow 普通粗体字，而是绿色/红色干刷笔触 + 原生分色文字。
- 价格区不再是规则矩形按钮，而是不规则红色笔刷丝带 + 暖黄笔触。
- 优惠区不再是纯色圆角块，而是绿色手绘贴片。
- 三类组件共享同一参考稿的深绿、品牌红、奶油白和手刷纹理。
- 菜品摄影仍是主视觉，标题/价格/优惠没有压成独立 UI 卡片。
- 文字层仍可精确修改，组件素材可单独重生成。

**仍存在的问题：**

- 视觉审查报告指出优惠贴片与右侧菜品/奶油纸边的局部关系可再优化。
- 标题与右上 Logo 的间距偏紧。
- 背景仍保留拍照式奶油边框，局部像拼贴；这是 SeeJoy 参考图的编辑拼贴语言，但若要求更“满版融合”，下一步应减少纸边框占比并让菜品出血。
- 视觉 Critic 本次给出 2 个 major issue，但保留初稿；没有自动执行修正动作。

## 7. 编辑能力评估

### 7.1 已具备

- 语义组件选择。
- 组件整体拖动。
- 组件整体缩放。
- 组件文字编辑。
- 图层显隐。
- 版本保存与恢复。
- 单组件视觉重生成。
- 服务端 PNG 导出。

### 7.2 尚未具备

- Fabric.js/Konva.js 原生画布编辑器。
- 浏览器端正式导出 PNG。
- 组件内部子图层自由拖动。
- 复杂艺术字与原生文字共用同一套前端渲染。
- 多候选构图与自动选优。

### 7.3 技术选择说明

当前 MVP 保留 **DOM 图层交互 + Pillow 权威渲染**：

- 优点：服务端输出稳定，中文文字、描边、阴影、透明通道与下载文件一致。
- 代价：拖动/缩放后需要短时间服务端重渲染；浏览器预览不是最终导出引擎。
- 下一阶段评估 Fabric.js/Konva.js 时，必须先解决字体加载、文字描边、旋转、透明 PNG 和浏览器导出与 Pillow 成品的一致性，否则会形成两套效果不同的渲染逻辑。

## 8. 风险与后续建议

1. **成本**：参考驱动模式一次生成使用 5 次图片调用，明显高于经典模式。建议在产品 UI 中显示调用预算，并保留经典模式。
2. **延迟**：首次生成约 7 分钟，适合异步任务，不适合同步等待。
3. **组件缓存**：已按 prompt + 参考图 hash + 品牌版本缓存；不同任务复用相同风格素材时可降低成本。
4. **局部重生成**：已可用，但建议增加“保留当前 / 接受新素材”对比选择，避免重生成结果劣化。
5. **评审闭环**：下一步应让 Critic 能建议 `regenerate_component`，并对组件级候选做 A/B 选择。
6. **前端画布**：Phase 2 再迁移 Fabric.js/Konva.js，优先验证浏览器导出与 Pillow 的像素级一致性。

## 9. 产物清单

- 最终海报：`storage/deliverables/final.png`
- 最终缩略图：`storage/deliverables/final-thumb.jpg`
- 全局参考：`storage/deliverables/design-reference.png`
- 干净底图：`storage/deliverables/clean-background.png`
- 标题笔刷：`storage/deliverables/component-title.png`
- 价格丝带：`storage/deliverables/component-price.png`
- 优惠贴片：`storage/deliverables/component-offer.png`
- 组件总览：`storage/deliverables/components-thumb.jpg`

## 10. 验收判定

| 验收项 | 结果 |
|---|---|
| 先整体设计，再逐层生成 | 通过 |
| 多次调用模型生成背景/组件 | 通过，6 次真实图片调用 |
| 所有调用共享同一全局参考 | 通过 |
| 标题明显更有设计感 | 通过 |
| 价格区不再是普通红色矩形 | 通过 |
| 背景与组件融合更自然 | 通过，仍有局部优化空间 |
| 关键业务文字可控 | 通过，改字 0 生图 |
| 单组件定向重生成 | 通过 |
| 结构化保存/恢复/导出 | 通过 |
| 不破坏现有编辑能力 | 通过，109 后端测试 + 前端 build/lint |
