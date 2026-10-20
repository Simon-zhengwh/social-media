# Visual-First Semantic Component System — Phase 0 / Phase 1 报告

日期：2026-10-20  
范围：真实代码分析 + 主标题/价格组件最小视觉实验；未进行全量架构重构。

## 1. 为什么当前结果仍像 UI 拼接

1. **背景与排版仍是两个独立决策。** `designer.build_background_prompt()` 负责底图，`designer.make_layout()` 只在底图生成后安排文字；即使共享 DesignIntent，布局仍以“找空位放文本”为主。
2. **文本与装饰是平铺的绝对坐标图层。** `renderer.render_design()` 逐层合成 text/shape/image，没有组件局部坐标系、组件内层级或统一视觉语言。标题和价格很容易退化成“文字 + 矩形底板”。
3. **布局模型会输出重复文案层。** 9:16 实验任务 `task_1bd2d1b1240a444b933f1fb10702f8a4` 中出现 10 个重复 headline 层（“周末/相聚”各 5 个），视觉上靠叠加阴影掩盖，实际是重复信息与噪声。
4. **角色别名被丢弃。** 模型使用 `promotion/subtitle/annotation` 时，旧代码只接受 `offer/subheadline/caption`，导致文案与初始对象计划丢失。
5. **shape 几何被压坏。** 模型用绝对 points 描述笔刷/徽章，旧 asset generator 却按默认 600×160 盒子处理，导致装饰堆在左上角。
6. **Critic 只能移动/改样式。** 它无法重写组件结构或重新生成局部视觉组件，遇到“标题不够有表现力”只能继续微调字号/位置。
7. **缺少低成本构图候选。** 每次生成只有一条完整链路，构图方向错误时只能整张重来。

## 2. 本次已落地的底层修复

- 角色别名：`subtitle → subheadline`、`promotion → offer`、`annotation → caption`。
- 按 `copy_id` 去重，禁止同一文案被重复绘制成多个文字层。
- shape 绝对 points 自动推导 box 并归一化，恢复笔刷/徽章/下划线的真实几何。
- 文本 `runs`：同一标题可原生渲染分色文字，不需要拆成不可控的重复图层。
- Visual Component 数据模型与 Pillow 渲染分支：
  - `type="visual_component"`
  - `semantic_type`
  - `render_mode: native | regeneratable | flattened`
  - `content`
  - `children`（text/shape/image 可混合）
  - 组件局部坐标、统一旋转/透明度与整体合成
- 浏览器真实生成、文字修改、Logo 移动、刷新恢复已验证；文字修改和 Logo 移动均未产生额外图片调用。

## 3. Phase 1 视觉实验

实验对象：`task_1bd2d1b1240a444b933f1fb10702f8a4`（9:16，两张商品图，真实 SeeJoy 背景）。  
实验只替换主标题与价格区域，背景与其他信息层尽量保持不变。

产物：

- `storage/visual-component-experiment/before.png`
- `storage/visual-component-experiment/after.png`
- `storage/visual-component-experiment/after-title-edit.png`
- `storage/visual-component-experiment/compare.png`
- `storage/visual-component-experiment/report.json`

组件结构：

- **标题组件**：两个原生 text child（深绿/品牌红）、白色粗描边、暖黄笔刷下划线、装饰点；组件整体轻微旋转。
- **价格组件**：不规则红色笔刷丝带、货币/数字/单位三个 text child、暖黄装饰线；修改价格内容时只重排组件内部文字。

实际观察：

- 标题从重复叠加的多层文本变成单一语义组件，分色和笔刷下划线更接近参考图的“手工贴纸”语言。
- 价格从“文字 + 常规矩形”变成不规则丝带 + 多层级数字/单位，视觉重心更明确。
- 标题改成“周末限时特惠”后重新渲染成功，说明组件内容替换与整体风格保持可以同时成立。
- 背景未被重新生成，商品图片和 Logo 引用保持不变。

仍需解决：

- 标题和价格的笔刷边缘目前是确定性几何，不是真正的手刷纹理；下一步可接透明艺术组件素材。
- 组件目前未进入前端编辑面板，尚未形成“语义组件级”的选中、移动、改字闭环。
- 多候选构图尚未实现，当前仍是一次完整生成后再 Critic 修正。

## 4. 真实调用观测

- 1:1 完整生成（首次 + Critic 修正）：约 229–365 秒。
- 9:16 双商品图完整生成：约 312 秒。
- 单张海报图片模型调用：1 次；设计/布局/审查对话调用约 4 次。
- 浏览器内文字修改与 Logo 移动：0 次图片调用。
- 测试：后端 106 个测试通过；前端 build/lint 通过。

## 5. 建议的下一阶段（需要确认后再做）

1. 将 Visual Component 正式接入 Scene v3 校验、保存、版本恢复和前端图层面板。
2. 增加组件级编辑：标题/价格内容修改后，按组件模板重建 children；组件整体移动/缩放/旋转。
3. 为标题、价格增加 `regeneratable` 模式：AI 生成透明笔刷/丝带素材，失败时回退到当前 native 组件。
4. 增加低成本构图候选（仅文字/组件草图，不先生成多张完整底图），再由 Critic 选择。
5. 在 Critic 中加入“矩形感/硬边界/风格割裂”检测和组件级修复动作。

在全量接入前，建议先用 3–5 个真实需求验证组件化是否稳定优于当前 native 文本层。
