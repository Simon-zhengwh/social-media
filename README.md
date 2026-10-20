# AI 视觉设计 Agent（首版）

从资产管理选择最多 4 张菜品/商品图与可选 Logo，填写提示词，由 ToAPIs 生成场景底图；设计模型规划文字层，Pillow 精确渲染文字，Logo 由模型自然融入并用原始透明素材做像素级覆盖。生成后可以继续用自然语言改字、改价格、调样式、移动 Logo 或换背景。

## 首版能力

- 左侧菜单包含「海报生成」和「资产管理」。
- 生成页提供品牌风格选择，默认选中 SeeJoy；品牌决定配色、字体、构图和 Logo 规则，任务创建后锁定。
- 品牌规范来自项目内 `.agents/skills/<brand>/`，新增品牌只需增加 skill 目录，前后端不需要改代码。
- 资产管理支持上传商品图 / Logo、载入 `素材/` 全部图片、查看已保存素材、一键“用于海报”和删除素材。
- 生成海报必须从资产管理选择商品图；Logo 可选，生成页不再直接上传文件。
- 商品图最多选择 4 张，多张图会融合成同一张海报，第一张作为主视觉。
- 支持 `1:1`、`9:16`、`4:5`、`16:9` 四种比例；`1:1` 使用 1K，其他比例使用 2K 生成并输出到 1080×1920 / 1080×1350 / 1920×1080。
- 资产管理列表接口：`GET /api/assets`（默认返回商品图和 Logo，可用 `?kind=` 过滤）。
- 一键载入 `素材/` 下的全部图片，也支持手动上传 JPEG/PNG/WebP。
- ToAPIs 异步生图：参考图上传 → 提交任务 → 轮询 → 下载底图。
- Logo 来自原始透明 PNG，缩放和位置由代码控制，不被模型重绘。
- 海报生成页提供提示词输入；提示词可以包含文案，留空使用默认暖色餐厅场景。文案会由设计模型抽取为独立文字层，而不是烘焙进底图。
- 生成后会拆成「底图 / 文字 / Logo」三类图层：底图独立版本；标题、副标题、价格、促销、活动信息、CTA、品牌名等均为真实文字层；Logo 保留原始素材引用。
- 多轮对话编辑：改文案、改价格、改字号/颜色/对齐、移动/缩放 Logo、重新生成底图。改文字只更新目标文字层，不调用生图；只有换背景指令才重新生成底图。
- 广告感排版兜底：如果排版模型把文字堆在左上角或价格位置过高，会自动切换到「超大标题 + 底部价格色带 + 倾斜促销徽章 + CTA 标签」的构图；也可以直接说“重新排版，广告感强一点”，不重新生图。
- 文字层支持轻微旋转，价格/促销/CTA 可带品牌色标签。
- 每次修改保存版本，可查看历史成品并恢复到任意版本。
- 图层列表支持选中、显示/隐藏、内联改字、字号/颜色/对齐、Logo 四角定位和缩放。
- 渲染后做基本可读性检查：文字溢出、文字与底图对比度过低会给出提示。
- 任务、供应商 ID 和设计数据保存到 MongoDB，原图、底图和成品保存到腾讯云 COS；MongoDB 只存对象键。刷新页面可通过 `?task=<id>` 恢复。
- 生成阶段、进度、失败原因、继续查询和下载。

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
TOAPIS_API_KEY=replace_me
TOAPIS_IMAGE_BASE_URL=https://api.toapis.com
TOAPIS_UPLOAD_BASE_URL=https://toapis.com
TOAPIS_IMAGE_MODEL=gpt-image-2.5-flare
TOAPIS_DESIGN_MODEL=gpt-6-sol-official
TOAPIS_EDIT_MODEL=deepseek-v4-flash
DESIGN_MODEL_ENABLED=true
LOGO_INTEGRATION_ENABLED=true
LOGO_EXACT_OVERLAY=true

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
| `GET /api/brands` | 返回可用品牌风格、封面 URL、色板与 Logo 匹配词 |
| `GET /api/brands/{id}/cover` | 返回品牌 skill 的封面参考图 |
| `PATCH /api/tasks/{id}/layers/{layer_id}` | 直接修改图层字段（文字内容、样式、位置、尺寸、可见性） |
| `GET /api/tasks/{id}/versions` | 版本列表和成品缩略图 |
| `POST /api/tasks/{id}/versions/{version}/restore` | 恢复历史版本，并保存为一个新版本 |
| `GET /api/tasks/{id}/download?version=n` | 下载指定版本 PNG |

编辑路由优先使用确定性规则处理「把 X 改成 Y」，保证用户原文不被模型改写；复杂指令才交给编辑模型。所有动作先校验目标图层和允许字段，再执行并重新渲染，底图与无关图层保持不变。

### 同步素材文件夹

`backend/scripts/sync_materials.py` 会删除 MongoDB 中所有 `product` / `logo` / `style` 素材及其 COS 对象，然后把 `素材/` 下的所有图片重新导入。Logo 文件按文件名中的 `logo` 识别。

```powershell
Set-Location backend
.\.venv\Scripts\python.exe scripts\sync_materials.py
```

当前 `素材/` 中包含：

- `seejoy火锅.png`、`seejoy门店.png`：商品/门店参考图。
- `logo.png`：白色透明 Logo。
- `seejoy_logo.png`：从 Google 图片搜索结果中的 SeeJoy 社媒头像获取并裁成透明圆形的红底 Logo。

渲染器会根据 Logo 亮度自动选择奶油色或深色底牌：白色 Logo 使用深色底牌，红底圆形 Logo 使用奶油色底牌。

### 品牌风格 Skill

品牌视觉规范放在 `.agents/skills/seejoy-brand-poster/`：

- `SKILL.md`：品牌说明、参考图索引、生成与验收检查表。
- `brand.json`：品牌 id、名称、封面、色板、字体映射、Logo 匹配词和指南路径。
- `references/layout-guide.md`：设计/排版模型使用的标题、构图、价格区和 Logo 规则。
- `references/background-guide.md`：生图模型使用的摄影、场景、光线和禁止项。
- `references/style-analysis.md`：三张参考图的逐张拆解。
- `assets/reference-1.jpg` 至 `reference-3.jpg`：品牌风格参考图。

后端启动时扫描 `SKILLS_DIR` 下的 `*/brand.json`。创建任务时把品牌规范快照写入任务，后续重新排版和换背景继续使用同一份规范。新增或修改品牌 skill 后重启后端即可生效。主标题使用随项目提供的 `backend/fonts/MaShanZheng-Regular.ttf`（SIL OFL），正文继续使用 Noto Sans SC。

## 测试

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest
Set-Location ..\frontend
npm run build
npm run lint
```

默认测试不调用付费模型。真实生图验证会消耗 ToAPIs 额度，1K 单张按文档约 $0.015。

本次多轮编辑与 Logo 融合的完整测试报告见 [`doc/test-report-2026-10-20.md`](doc/test-report-2026-10-20.md)，实现说明见 [`doc/multiturn-edit.md`](doc/multiturn-edit.md)。

## 目录

```text
backend/            FastAPI、生图服务、渲染器、任务流水线
frontend/           React + TypeScript 单页工作台
.agents/skills/     品牌风格 skill（SeeJoy 及后续品牌）
storage/            本地日志、验证截图；未配置 COS 时的本地对象回退
素材/               示例菜品图与 Logo
docker-compose.yml  MongoDB 8.0，127.0.0.1:27018
```

## 已知边界

- 生图以菜品图为参考重建场景，菜品细节可能变化；原始素材始终单独保留，便于人工对照。
- 文字由 Pillow 渲染，不再由生图模型直接写字；长文案会自动缩小，极端情况下仍可能溢出，界面会提示。
- Logo 先由模型融入局部底图，再把原始透明 Logo 精确覆盖在预留位置；这样既有环境融合，也不会让模型改写品牌字形。模型融合失败时自动退化为透明叠加，不会出现底牌或白底。
- 换背景会保留文字和 Logo 图层，但新底图可能降低文字对比度；渲染后会提示，需要用户决定是否调整文字颜色/阴影。
- 多图融合可能改变单个商品的数量、摆盘或细节；需要严格保真时仍应优先使用单张商品图。
- 生图 1K/1:1 实际返回约 1254×1254；9:16 等比例使用 2K 生成，后端会等比缩放到目标画布。
- 删除素材会同时删除 MongoDB 记录和 COS 对象；删除后旧任务可能无法恢复缩略图，但历史成品仍保留。
