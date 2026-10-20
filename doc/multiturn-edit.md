# 多轮编辑与分层实现说明

> 版本：2026-10-20  
> 对应代码：`backend/app/services/{design,designer,editor,renderer,pipeline,quality}.py`  
> 目标：把 `design.md` 的「Agent 自由设计 + 最小必要分层 + 多轮精准编辑」落成可运行实现。

## 1. 为什么不能再把 Logo 直接贴在代码底牌上

旧实现用 Pillow 在左上角画一个不透明圆角矩形，再把 Logo 叠上去。问题有三个：

1. 圆角底牌是代码固定生成的，和底图的光线、材质、构图无关，视觉上像贴纸；
2. 底牌不透明，白色/浅色 Logo 会被迫依赖底牌才能看清，违背用户提供透明 Logo 的意图；
3. 位置固定，设计 Agent 没有参与品牌元素的构图。

新实现分两步：

1. **模型局部融合**：裁出 Logo 预留位置周围的局部底图，连同透明 Logo 原图交给生图模型，要求把 Logo 自然融入该局部，不允许加卡片/白底/边框；
2. **原始素材精确覆盖**：把模型融合后的局部贴回底图，再用原始透明 Logo 在预留框内等比覆盖。模型负责环境光影，原始文件负责品牌字形和透明边缘。

如果局部融合失败（超时、模型返回无变化、Logo 位置不符），自动退化为纯透明叠加，不出现底牌。

## 2. 图层模型

设计状态保存在任务文档的 `design` 字段，同时每个版本保存完整快照：

```json
{
  "schema_version": 2,
  "canvas": { "width": 1080, "height": 1920 },
  "background": {
    "clean_asset_id": "asset_clean",
    "render_asset_id": "asset_logo_integrated",
    "prompt": "夜晚餐厅...",
    "logo_integrated": true
  },
  "layers": [
    { "id": "background", "type": "background", "asset_id": "asset_logo_integrated", "locked": true },
    {
      "id": "text_headline",
      "type": "text",
      "role": "headline",
      "name": "主标题",
      "content": "夜宵时间到",
      "position": { "x": 70, "y": 140 },
      "size": { "width": 900, "height": 220 },
      "style": {
        "font_size": 150,
        "font_weight": 900,
        "color": "#FFF5DC",
        "align": "left",
        "stroke": { "width": 6, "color": "#8F1712" },
        "shadow": { "blur": 12, "offset_x": 0, "offset_y": 6, "color": "#00000088" }
      }
    },
    {
      "id": "logo_brand",
      "type": "logo",
      "asset_id": "asset_logo",
      "position": { "x": 890, "y": 50 },
      "size": { "width": 140, "height": 140 },
      "mode": "model_integrated"
    }
  ]
}
```

关键点：

- `background.clean_asset_id` 永远是不含 Logo 的干净底图；
- `background.render_asset_id` 是当前用于渲染的底图（可能已融合 Logo）；
- 文字层的内容、坐标、样式、字号都独立保存；
- Logo 层只引用原始资产，不保存重绘结果；
- `mode=model_integrated` 表示 Logo 已烘焙进 `render_asset_id`；`mode=overlay` 表示渲染时用透明叠加。

## 3. 首次生成流水线

```text
素材 + 提示词
  → 设计模型抽取文案 / 生成底图提示词（不写字、不画 Logo）
  → ToAPIs 生成干净场景底图
  → 设计模型看实际底图，输出文字层 boxes / 字号 / 颜色 / Logo 框
  → 校验并解决 Logo 与文字重叠、Logo 原始宽高比
  → 模型局部融合 Logo + 原始透明 Logo 精确覆盖
  → Pillow 渲染文字层
  → 可读性检查
  → 保存 v1（底图、设计状态、成品、质量报告）
```

底图提示词强制包含「不要出现任何文字、字母、数字、Logo、二维码、水印」。正式文案由文字层渲染，避免生成式错字。

### 3.1 广告感排版兜底

排版模型可能为了避让菜品把全部文字堆在左上角，形成“PPT 文字列表”。`designer.repair_layout` 会检查：

- 文字最大底边是否低于画布 70%；
- 价格是否位于画布 55% 以下；
- 标题字号、促销/价格标签是否具备广告层级。

不满足时自动切换 `promo_layout`：超大标题、底部价格色带、倾斜促销徽章、CTA 小标签、活动信息底部、Logo 右上。文字层支持 `rotation`，色块通过文字层的 `background` 实现，不额外引入复杂图层。

也可以直接对已有成品说“重新排版，广告感强一点”，只重排文字层，不重新生成底图。

## 4. 多轮编辑流水线

```text
用户指令
  → 识别目标图层与动作
  → 校验：图层存在、字段允许、只改目标
  → 文字/样式/位置：直接重渲染
  → Logo 位置/尺寸：从 clean 底图重新局部融合 + 精确覆盖
  → 背景：重新生成 clean 底图，文字层和 Logo 层原样保留
  → 保存新版本
```

### 4.1 确定性优先

「把价格改成 169 元」这类有明确目标和新文案的指令，先走规则解析：

- 按标点拆成多个子句，支持一次改多个字段；
- 新文案取自用户原文，模型不能追加「/位」等后缀；
- 只允许 `update_text` 等白名单动作；
- 模型改写文案会被拒绝并回退到规则解析。

复杂指令（例如没有明确目标、需要理解上下文）才交给编辑模型 `deepseek-v4-flash`，输出动作计划后再做同一套校验。

### 4.2 允许的动作

| 动作 | 作用 | 是否调用生图 |
|---|---|---|
| `update_text` | 改指定文字层内容 | 否 |
| `update_text_style` | 改字号/颜色/对齐/字重 | 否 |
| `move_layer` / `resize_layer` | 移动/缩放文字或 Logo | Logo 需要重新融合，文字否 |
| `set_visibility` | 显示/隐藏图层 | 否 |
| `regenerate_background` | 重新生成场景底图 | 是 |
| `add_text` / `delete_layer` | 新增/删除文字层 | 否 |
| `replace_logo_asset` | 替换 Logo 素材 | Logo 重新融合 |

## 5. 不变量

以下不变量由 `backend/tests/test_design.py`、`test_designer.py`、`test_quality.py`、`test_pipeline.py` 覆盖：

1. 改文字只改变目标文字层的 `content`；底图、Logo、其他文字层逐字段相等；
2. 改样式只改变目标文字层 `style`（以及可选的同层 box 缩放）；
3. 移动 Logo 不改文字层、不改 `clean_asset_id`；
4. 换背景只改 `background.clean_asset_id` / `render_asset_id` / `background.prompt`，文字层和 Logo 层完全不变；
5. 隐藏或删除已融合 Logo 时，渲染底图切回 `clean_asset_id`，避免 Logo 残留；
6. 每次修改生成新版本，旧版本成品对象不被覆盖；
7. 恢复历史版本会重新渲染并保存为新版本，不删除历史；
8. 输入设计状态不会被渲染器或动作执行器原地修改。

## 6. 渲染器

`render_design(settings, background, layers, canvas_size, assets)`：

- 按 `z_index` 升序合成；
- 文字使用 `NotoSansSC-VF.ttf` 可变字重，支持中英文换行、显式换行、字间距、对齐、描边、阴影、透明度、可选背板；
- 超出文本框时逐步缩小字号，最低到初始字号的 35%；仍放不下标记 `overflow=true`，不截断；
- Logo overlay 只做 alpha 合成，绝不添加底板/卡片/白边；
- 返回每个文字层的实际 box、字号、行数、overflow，用于质量检查和前端边界框。

## 7. 质量检查

`quality.assess_legibility` 在每次渲染后运行：

- 文字溢出：渲染器标记 `overflow`；
- 文字对比度：取文本框内底图平均亮度与文字/描边颜色计算对比度，低于 2.5:1 时警告；
- 结果写入任务和版本，前端在预览下方显示提示。

注意：质量检查只提示，不自动改用户没有授权的文字样式。

## 8. API

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/tasks` | 创建任务 |
| `GET` | `/api/tasks/{id}` | 任务、设计状态、消息、版本号、编辑状态、质量报告 |
| `POST` | `/api/tasks/{id}/edits` | 自然语言修改，202；轮询 `edit.status` |
| `PATCH` | `/api/tasks/{id}/layers/{layer_id}` | 直接改图层字段 |
| `GET` | `/api/tasks/{id}/versions` | 版本列表 |
| `POST` | `/api/tasks/{id}/versions/{n}/restore` | 恢复版本 |
| `GET` | `/api/tasks/{id}/download?version=n` | 下载当前/历史版本 |

## 9. 已知边界

- 局部融合仍可能改变 Logo 周围的光影；精确覆盖保证品牌字形不变，但极端背景下边缘可能需要人工微调；
- 换背景后文字样式保持不变，可能出现对比度不足；系统只提示，不自动改；
- 画布暂不支持拖拽，位置调整通过图层四角按钮或对话完成；
- 布局模型仍可能给出不理想的文字 density；Logo 重叠和宽高比已由代码兜底；
- 首次生成需要两次生图（底图 + Logo 局部融合），成本和耗时高于旧版。
