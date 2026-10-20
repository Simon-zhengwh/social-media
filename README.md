# AI 视觉设计 Agent（V2 · Phase 1A + 1B）

从资产管理选择最多 4 张菜品、商品或普通参考图，填写提示词，由 core-ai 网关生成场景底图；设计模型规划文字层，Pillow 精确渲染文字。Logo 如需要，直接作为普通参考图选择，不进入特殊 Logo 图层或融合链路。生成后可以继续用自然语言改字、改价格、调样式或换背景。


## 两种工作流

- **海报生成（分层）**：保留设计规划、文字层和 Pillow 合成能力。
- **自由设计（整图）**：新增的轻量流程，不拆分图层，模型直接生成和编辑完整图片；每次成功操作保存独立版本，可切换历史版本继续编辑。

左侧菜单可直接在两种工作流之间切换，二者使用独立的任务与版本集合，互不影响。

### 自由设计接口

| 接口 | 用途 |
|---|---|
| `POST /api/whole-image/tasks` | 创建整图任务 |
| `GET /api/whole-image/tasks/{id}` | 恢复整图任务和当前版本 |
| `POST /api/whole-image/tasks/{id}/edits` | 基于 `source_version_id` 编辑图片 |
| `GET /api/whole-image/tasks/{id}/versions` | 获取整图版本 |
| `POST /api/whole-image/tasks/{id}/versions/{version_id}/select` | 选择继续编辑的版本 |
| `GET /api/whole-image/tasks/{id}/download?version_id=...` | 下载整图版本 |

整图流程使用 `gpt-image-2.5-flare` 调用 core-ai `/images/generations` 和 `/images/edits`，不调用旧的分层规划、文字渲染或 Pillow 合成链路。

## 首版能力

- 左侧菜单包含「海报生成」和「资产管理」。
- 生成页提供品牌风格选择，默认选中 SeeJoy；品牌决定视觉规范、构图和素材呈现，任务创建后锁定。
- 品牌规范保存在 MongoDB `brands` 集合；`.agents/skills/<brand>/` 仅用于首次导入和人工文档，运行时不读取。
- 左侧「品牌风格」页面支持卡片列表、详情编辑、指南修改，以及品牌参考图上传、删除和封面设置；参考图不进入资产库。
- 资产管理支持上传图片、载入 `素材/` 全部图片、查看已保存素材、一键“用于海报”和删除素材。
- 生成海报必须从资产管理选择商品图或普通参考图；Logo 文件也按普通图片处理。
- 商品图最多选择 4 张，多张图会融合成同一张海报，由模型根据构图自由安排素材层次。
- 每次可生成 1–3 张候选；完成后在结果区选定一张继续编辑，其他候选保留在同一批次中，可随时切换、回退或导出。
- 生成页先选「发布平台」再选「发布用途」，比例与尺寸内置在平台预设中，不再手动选择比例。当前包含小红书、Instagram、TikTok、Google Business Profile 和 Meta 广告；支持 3:4、4:3、1:1、4:5、9:16、16:9 画布。
- 平台规则通过 `GET /api/platforms` 统一提供，任务创建时保存平台、用途和安全区快照。小红书不展示地址、电话、微信或二维码；其他平台必须填写并展示商家地址和联系电话。
- 成品尺寸由平台预设决定，网关尺寸按 16 像素对齐后同步提交，例如 1080×1920 实际发送 `1072x1920`，最终导出仍为 1080×1920。
- 资产管理列表接口：`GET /api/assets`（默认返回可用图片素材，可用 `?kind=` 过滤）。
- 一键载入 `素材/` 下的全部图片，也支持手动上传 JPEG/PNG/WebP。
- core-ai 同步生图 / 改图：参考图优先使用 COS 预签名公网 URL，本地对象或无公网地址时回退为 data URL，再提交 `/images/generations` 或 `/images/edits`。设计、排版和自然语言编辑走 `/chat/completions`。
- 海报生成页提供提示词输入；提示词可以包含文案，留空使用默认暖色餐厅场景。文案会由设计模型抽取为独立文字层，而不是烘焙进底图。
- 生成后会拆分底图、真实文字层和图片层；标题、副标题、价格、促销、活动信息、CTA、品牌名等均为可编辑文字层。
- 多轮对话编辑：改文案、改价格、改字号/颜色/对齐、移动/缩放图片或重新生成底图。改文字只更新目标文字层，不调用生图；只有换背景指令才重新生成底图。
- 自由构图：Creative Plan 先输出共享 DesignIntent、copy_items、素材请求和初始对象计划；背景生成与布局校准使用同一份意图，不再因为价格不在下半部或版面不像促销模板而自动替换固定版式。
- 文字层支持旋转、同角色多图层和稳定 ID；价格/促销/CTA 可按设计意图使用品牌色标签。
- Scene v3：`text`、`image`（商品/参考图/装饰）和简单 `shape` 可独立渲染、显示和调整。
- 视觉审查：首轮成图会交给 Visual Critic 做一次受限审查和最多一次样式/几何修正；修正稿更差或 Critic 不可用时自动保留初稿。
- 生成页要求先选择至少一张商品图；商品图是必选素材。
- 每次修改保存版本，可查看历史成品并恢复到任意版本。
- 图层列表支持选中、显示/隐藏、内联改字、字号/颜色/对齐、图片定位和缩放。
- 渲染后做基本可读性检查：文字溢出、文字与底图对比度过低会给出提示。
- 任务、`provider` 元数据、DesignIntent、候选和设计数据保存到 MongoDB，原图、品牌参考、底图和成品保存到腾讯云 COS；MongoDB 只存对象键。刷新页面可通过 `?task=<id>` 恢复。
- 品牌参考图在任务创建时保存为不可变 `brand_reference` 资产并记录 sha256，历史任务不会因品牌目录更新而改变参考输入。
- 用户直接修改图层后会设置 `ai_locked`，后续 AI 编辑和 Critic 默认跳过该图层。
- 生成阶段、进度、失败原因、恢复和下载。

首版不包含自动抠图、画布拖拽、多尺寸套图、账号体系和协同编辑。

## 环境

- Node.js 22.x
- Python 3.12（推荐用 `uv` 管理）
- Docker Desktop + Docker Compose

## 启动

在项目根目录启动 MongoDB（端口映射到 `27018`，避免占用本机 27017）：

```powershell
docker compose up -d
docker compose ps
docker exec social-media-mongo mongosh --quiet --eval "db.adminCommand({ ping: 1 })"
```

后端：

```powershell
uv python install 3.12
uv venv --python 3.12 backend/.venv
uv pip install --python backend/.venv/Scripts/python.exe -r backend/requirements-dev.txt
Set-Location backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

前端：

```powershell
Set-Location frontend
npm install
npm run dev -- --host 127.0.0.1
```

打开 http://127.0.0.1:5173 ，进入「资产管理」载入示例素材，再回到「海报生成」选择素材并生成。

后端接口文档：http://127.0.0.1:8000/docs

## 配置

`backend/.env` 保存本地密钥，已被 `.gitignore` 排除；不要提交。关键项：

```dotenv
MONGODB_URI=mongodb://127.0.0.1:27018
MONGODB_DATABASE=social_media
STORAGE_DIR=../storage
SAMPLE_DIR=../素材
SKILLS_DIR=../.agents/skills
CORE_AI_GATEWAY_BASE_URL=https://core-ai-server.connexup-uat.net/api/gateway/v1
CORE_AI_API_KEY=replace_me
CORE_AI_IMAGE_MODEL=gpt-image-2.5-flare
CORE_AI_DESIGN_MODEL=gpt-6.1-sol
CORE_AI_EDIT_MODEL=deepseek-v4-flash
CORE_AI_IMAGE_QUALITY=high
CORE_AI_IMAGE_TIMEOUT_SECONDS=300
DESIGN_MODEL_ENABLED=true
VISUAL_CRITIC_ENABLED=true
LLM_TIMEOUT_SECONDS=120

COS_SECRET_ID=replace_me
COS_SECRET_KEY=replace_me
COS_BUCKET=simon-1349795454
COS_REGION=ap-guangzhou
COS_PREFIX=social-media
```

图片对象路径形如 `social-media/prepared/asset_xxx.png`、`social-media/backgrounds/task_xxx_clean.png`、`social-media/backgrounds/task_xxx_v1_logo.png`、`social-media/outputs/task_xxx_v1.png`。每次编辑的输出和底图都使用独立对象键，历史版本不会被覆盖。浏览器预览和下载通过后端 302 到 COS 预签名 URL，成品下载带 `content-disposition: attachment`。未配置 COS 时会退回本地 `storage/objects/`，仅用于离线测试；正式运行请配置 COS。

## 多轮编辑接口

| 接口 | 用途 |
|---|---|
| `POST /api/tasks/{id}/edits` | 自然语言修改，返回 202；轮询任务直到 `edit.status` 变为 `idle`/`failed` |
| `GET /api/platforms` | 返回发布平台、用途、内置尺寸、安全区和联系方式规则 |
| `GET /api/brands` | 返回可用品牌风格、封面 URL 与 Logo 匹配词 |
| `GET /api/brands/{id}` | 返回品牌完整配置和参考图列表 |
| `PUT /api/brands/{id}` | 更新品牌名称、指南和默认状态 |
| `POST /api/brands/{id}/references` | 上传品牌参考图，最多 3 张 |
| `DELETE /api/brands/{id}/references/{asset_id}` | 从品牌配置中移除参考图 |
| `GET /api/brands/{id}/cover` | 返回 MongoDB 品牌配置的封面参考图 |
| `PATCH /api/tasks/{id}/layers/{layer_id}` | 直接修改图层字段（文字内容、样式、位置、尺寸、可见性） |
| `GET /api/tasks/{id}/versions` | 版本列表和成品缩略图 |
| `POST /api/tasks/{id}/versions/{version}/restore` | 恢复历史版本，并保存为一个新版本 |
| `GET /api/tasks/{id}/download?version=n` | 下载指定版本 PNG |

编辑路由优先使用确定性规则处理「把 X 改成 Y」，保证用户原文不被模型改写；复杂指令才交给编辑模型。所有动作先校验目标图层和允许字段，再执行并重新渲染，底图与无关图层保持不变。

### 同步素材文件夹

`backend/scripts/sync_materials.py` 会删除 MongoDB 中所有图片素材及其 COS 对象，然后把 `素材/` 下的所有图片作为普通图片重新导入。

```powershell
Set-Location backend
.\.venv\Scripts\python.exe scripts\sync_materials.py
```

当前 `素材/` 中包含：

- `seejoy火锅.png`、`seejoy门店.png`：商品/门店参考图。
- `logo.png`、`seejoy_logo.png`：现在与其他图片一样作为普通参考素材导入。

### 品牌配置（MongoDB）

品牌运行时配置保存在 MongoDB 的 `brands` 集合，`GET /api/brands` 直接读取该集合。`.agents/skills/seejoy-brand-poster/` 只用于首次导入和人工文档；`SKILL.md` 不参与运行时。

`brands` 文档主要字段：

```json
{
  "_id": "seejoy",
  "name": "SEEJOY 喜聚",
  "short_name": "喜聚",
  "summary": "深森林绿、品牌红与奶油白的餐饮促销海报",
  "is_default": true,
  "layout_guide": "排版指南全文",
  "background_guide": "背景指南全文",
  "logo_keywords": ["seejoy", "喜聚"],
  "cover_asset_id": "asset_xxx",
  "reference_asset_ids": ["asset_1", "asset_2", "asset_3"]
}
```

参考图和封面存放在 `assets` 集合与对象存储，Mongo 只保存 asset id，`kind` 为 `brand_reference`。品牌配置不维护版本号，保存即覆盖当前最新配置。

首次迁移或重新导入 skill：

```powershell
Set-Location backend
.\.venv\Scripts\python.exe scripts\import_brands.py --skills-dir ..\.agents\skills
```

默认只导入不存在的品牌；修改 skill 后需要覆盖时加 `--force`，只导入某个品牌时加 `--only seejoy`。

运行时没有品牌进程缓存，直接修改 Mongo 后下一个新任务立即生效：

```javascript
db.brands.updateOne(
  { _id: "seejoy" },
  { $set: { layout_guide: "新的排版规范" } }
)
```

任务创建时会把品牌配置和参考图 asset id 写入 `tasks.brand` 快照，旧任务不会因品牌配置变化而改变。没有快照的历史任务会按 `input.brand_id` 查 Mongo，查不到时使用代码内置的 SeeJoy 默认值。

左侧「品牌风格」页面是品牌配置的日常维护入口：卡片进入详情后可以改名称、简介、排版指南和背景指南，也可以上传/删除参考图或设置封面。修改保存后无需重启，下一个新任务立即使用新配置。

主标题使用随项目提供的 `backend/fonts/MaShanZheng-Regular.ttf`（SIL OFL），正文继续使用 Noto Sans SC。

## 测试

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest
Set-Location ..\frontend
npm run build
npm run lint
```

默认测试不调用付费模型。真实生图验证会消耗 core-ai 网关额度，验收时只执行一次海报生成和一次纯文字修改，避免重复调用图像模型。

本次多轮编辑与 Logo 融合的历史测试报告见 [`doc/test-report-2026-10-20.md`](doc/test-report-2026-10-20.md)，实现说明见 [`doc/multiturn-edit.md`](doc/multiturn-edit.md)。

Phase 1A + 1B 的五类真实基准使用显式付费开关：

```powershell
Set-Location backend
.\.venv\Scripts\python.exe scripts\benchmark_posters.py --live
```

报告默认写入 `storage/benchmarks/<run-id>/`，包含 final/baseline PNG、320px JPEG 缩略图、`report.json` 和 `report.md`。

## 目录

```text
backend/            FastAPI、生图服务、渲染器、任务流水线
frontend/           React + TypeScript 单页工作台
.agents/skills/     品牌 skill 种子与文档（运行时从 MongoDB brands 读取）
storage/            本地日志、验证截图；未配置 COS 时的本地对象回退
素材/               示例菜品图与参考图
docker-compose.yml  MongoDB 8.0，127.0.0.1:27018
```


## Reference-Guided Layer Generation（2026-10-20）

生成页固定使用「参考驱动分层」，不再展示经典分层入口：

1. Design Director 先输出 DesignIntent、文案与组件职责。
2. 图片模型先生成一张完整全局参考稿，后续背景和组件调用都引用同一张参考。
3. 干净底图保留完整食物场景，参考驱动模式不再叠加原始商品图或大面积照片卡片边框，避免硬切边界。
4. 标题/价格/优惠按依赖顺序生成：标题先完成并合成，价格和优惠再并行读取“全局参考 + 当前合成预览 + 局部周围裁剪 + 目标区域”生成。
5. 平台尺寸、安全区和联系方式规则会同时进入规划、排版与生图提示；带平台的任务在提交版本前额外检查安全区和联系方式，小红书出现联系信息或必填平台漏掉地址电话时会拒绝该结果。
6. Pillow 只做品红抠图、透明通道、色温匹配、裁切、缩放与合成；标题、价格、优惠文字仍是真实文字层。
7. 标题/价格/优惠分别组成 `visual_component`，支持 `compositing`（blend mode、opacity、feather、shadow、color_match、scene anchor）与组件关系元数据。
7. `POST /api/tasks/{id}/components/{component_id}/regenerate` 使用同一份上下文，只重生成目标组件素材，不触碰其他图层。
8. 价格组件不接收可读价格文本，只引用无文字笔刷材质；生成后检测大面积奶油字形区域，避免模型把旧价格烘焙进素材并与原生文字重叠。
9. 组件生成按 prompt + 参考图 + 当前合成上下文 + 品牌配置缓存，失败自动回退到原生文字/确定性装饰路径。

同一任务实测：全局参考 1 次、干净底图 1 次、三类组件各 1 次生图；组件级重生成 1 次生图。改字、拖动、缩放、版本恢复和导出均不调用图片模型。完整报告见 [`doc/reference-guided-layer-report-2026-10-20.md`](doc/reference-guided-layer-report-2026-10-20.md)。

当前 MVP 的画布交互采用 DOM 图层框 + 服务端权威渲染：支持选择、拖动、缩放、改字和组件重生成；正式导出仍由 Pillow 保证服务端一致性。Fabric.js/Konva.js 迁移保留为 Phase 2，需先解决画布字体、复杂描边和浏览器导出与后端渲染的一致性。

## 已知边界

- 生图以菜品图为参考重建场景，菜品细节可能变化；原始素材始终单独保留，便于人工对照。
- 文字由 Pillow 渲染，不再由生图模型直接写字；长文案会报告溢出，不再静默截断。极端情况下仍需人工调整。
- 本版本不做 Fabric 拖拽画布、分组、滤镜、渐变、复杂艺术字和自动抠图。
- 换背景会保留文字和图片图层，但新底图可能降低文字对比度；渲染后会提示，需要用户决定是否调整文字颜色/阴影。
- 多图融合可能改变单个商品的数量、摆盘或细节；需要严格保真时仍应优先使用单张商品图。
- 网关按对齐后的 `WxH` 直接生成（如 `1072x1920`），返回图片会等比缩放到目标画布。
- 删除素材会同时删除 MongoDB 记录和 COS 对象；删除后旧任务可能无法恢复缩略图，但历史成品仍保留。

## Visual-First 组件实验

Phase 1 的最小实验已落地：Scene 支持 `visual_component`（native/regeneratable/flattened），Pillow 支持组件局部坐标与 children 合成；标题/价格实验脚本：

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m scripts.visual_component_experiment
```

产物在 `storage/visual-component-experiment/`。完整分析见 [`doc/visual-first-phase1-2026-10-20.md`](doc/visual-first-phase1-2026-10-20.md)。
