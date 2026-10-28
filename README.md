# AI 视觉设计 Agent

从资产管理选择最多 6 张素材图，结合品牌风格、发布平台和设计提示词，由 core-ai 图片模型直接生成完整设计图；生成后可继续用自然语言改图，每次成功操作保存为独立版本。

当前只保留“海报设计（整图）”工作流：模型直接输出完整图片，不做图层拆分、文字层规划或 Pillow 合成。

## 主要能力

- 左侧菜单包含「海报设计」「任务中心」「修改海报」「资产管理」「品牌风格」。
- 「修改海报」是轻量 Agent 工作流：上传或选择 1–5 张参考图，输入修改要求，并一次并行生成 1–6 个候选；每个候选包含透明 PNG 和最终图。
- 海报设计支持选择预设品牌、参考图风格或无品牌模式。
- 品牌规范保存在 MongoDB `brands` 集合；`.agents/skills/<brand>/` 仅用于首次导入和人工文档，运行时不读取。
- 每套品牌风格必须绑定一个发布平台及平台预设的发布用途；可从现有风格复制出独立平台版本。
- 品牌风格页面支持紧凑基本信息编辑、生成规范折叠编辑、参考图管理，以及最多 5 张 Logo 和最多 6 个主色调管理。颜色统一使用 `#RRGGBB` HEX。
- 品牌风格支持按固定表单新建和编辑：配色说明、字体、主体表现、背景、排版、装饰、固定识别元素与专属要求；规范预览由后端统一组装。
- 「通用规范」可在品牌风格页编辑，覆盖文案准确、素材使用、Logo 和可读性。更新应用于新任务；任务保存当时的规范正文和版本，历史任务不变。
- 资产管理支持上传图片、查看已保存素材和删除素材。
- 每次生成支持 1–6 张候选，默认 3 张；候选可在结果区切换，选中的版本继续编辑。
- 发布平台为选填项：选择平台后可指定用途，比例、尺寸和安全区规则由平台预设统一提供；不选择时可手动指定画幅比例。
- 当前包含小红书、Instagram、TikTok、Google Business Profile 和 Meta 广告，支持 3:4、4:3、1:1、4:5、9:16、16:9 画布。
- 小红书平台预设禁止出现地址、电话号码和二维码；用户文案包含相关内容时，生成提示词会要求省略。
- 支持 `gpt-image-2.5-sunburst`、`gemini-3.1-flash-image`、`seedream-5-pro-image-to-image`，默认使用 `gpt-image-2.5-sunburst`。
- 提示词输入框支持 AI 优化，会根据当前品牌、素材、平台规则和商家信息生成可直接用于整图生成的提示词。
- 优化 Agent 使用 `.agents/skills/image-gen-prompt/`：从官方 `imagegen` skill 抽取提示词编写原则、可选字段和示例，按任务需要组织提示词。
- 图片默认保存在项目内 `local_media/objects/`；提示词优化按需把本地图片上传到自建图床，使用永久 443 公网直链供外部模型读取。
- 接口返回 `{"prompt":"..."}`，保留非空和长度校验；不要求固定七段、独立事实数组或案例专用的设计规则。优化后的提示词直接交给生图模型。
- 生成调用 core-ai `/images/generations`；自然语言改图并行调用 `/images/edits` 和图片 Agent，两个结果并存以便对比。
- 图片 Agent 不使用 attachments：social-media 先把当前图上传到 Core AI blob 或使用 COS 签名 URL，再把 URL 拼进 Agent `input`，通过 `trigger + get` 获取产物。
- 接口改图继续发送本轮修改要求、原始内容素材（含选定 Logo）和选中版本的生成图；Agent 改图只接收当前选中图 URL 和本轮修改要求。
- Agent 改图只传递本轮用户修改要求和按顺序排列的图片 URL，不再额外拼接画布尺寸、保留内容或输出格式等固定模板；具体执行由 `image-gen-skill` 自主完成。
- 任务、版本、候选和模型输入快照保存在 MongoDB；图片以本地对象存储为主，保存到项目内 `local_media/objects/`。
- 「修改海报」在 Agent 触发后立即保存 `run_id`；服务重启时会继续轮询已有 run，不会重新发起或丢失已完成的 Agent 结果。
- 侧边栏「任务中心」按候选批次聚合展示生成记录，可筛选状态、下载成品、页内重试失败项，并点击任务回到工作台继续编辑。
- 一次改图保存为一个版本，包含接口结果、Agent 透明层和 Agent 最终图；存在多个可选结果时必须手动选择后才能继续修改。
- 改图输出会归一化回源图的精确像素尺寸和比例；直接 Agent 修改以第一张参考图的尺寸作为输出画布。
- 刷新页面可通过 `?whole=<task_id>` 恢复整图任务和当前版本。
- 任务中心使用 `?view=tasks`；打开任务后会切换到 `?view=whole&whole=<task_id>`（整图页省略 `view` 参数）。

## 工作流接口

| 接口 | 用途 |
|---|---|
| `POST /api/whole-image/tasks` | 创建整图任务，返回 202 |
| `GET /api/whole-image/tasks?status=...&limit=...&cursor=...` | 按候选批次获取任务列表 |
| `GET /api/whole-image/tasks/{id}` | 获取任务、当前版本和模型输入 |
| `POST /api/whole-image/tasks/{id}/retry` | 重试失败的生成或改图任务，返回 202 |
| `POST /api/whole-image/tasks/{id}/edits` | 基于 `source_version_id` 执行自然语言改图 |
| `GET /api/whole-image/tasks/{id}/versions` | 获取版本列表 |
| `POST /api/whole-image/tasks/{id}/versions/{version_id}/select` | 选择继续编辑的版本 |
| `GET /api/whole-image/tasks/{id}/download?version_id=...` | 下载指定版本 |
| `GET /api/whole-image/batches/{batch_id}` | 获取候选批次 |
| `POST /api/whole-image/tasks/{id}/versions/{version_id}/outputs/{output_id}/select` | 选择下一轮编辑源图 |
| `POST /api/poster-edits/batches` | 创建 1–6 个并行 Agent 修改候选 |
| `GET /api/poster-edits/batches/{batch_id}` | 获取修改候选批次 |
| `POST /api/poster-edits` | 创建单个 Agent 修改任务（兼容接口） |
| `GET /api/poster-edits/{id}` | 获取直接 Agent 修改结果 |

公共接口：

| 接口 | 用途 |
|---|---|
| `GET /api/assets` | 获取资产库中的全部上传图片 |
| `POST /api/assets` | 上传图片素材 |
| `DELETE /api/assets/{id}` | 删除素材；被整图任务引用时需显式 `force=true` |
| `GET /api/brands` / `GET /api/brands/{id}` | 品牌列表与详情 |
| `PUT /api/brands/{id}` | 更新品牌配置 |
| `POST /api/brands` | 新建结构化品牌风格 |
| `POST /api/brands/preview` | 预览未保存的风格、通用规范及平台规则 |
| `GET /api/brand-rules` / `PUT /api/brand-rules` | 查看和更新通用规范，更新需携带当前版本 |
| `POST /api/brands/{id}/duplicate` | 复制为另一个平台/用途的独立品牌风格 |
| `POST /api/brands/{id}/references` | 上传品牌参考图，最多 5 张 |
| `DELETE /api/brands/{id}/references/{asset_id}` | 移除品牌参考图 |
| `POST /api/brands/{id}/logos` | 批量上传品牌 Logo，最多 5 张 |
| `DELETE /api/brands/{id}/logos/{asset_id}` | 从当前风格移除 Logo 关联 |
| `GET /api/platforms` | 平台、用途、尺寸和安全区规则 |
| `POST /api/prompts/optimize` | 优化整图生成提示词 |
| `GET /api/health` | MongoDB、对象存储和模型配置健康检查 |

## 环境

- Node.js 22.x
- Python 3.12
- Docker Desktop + Docker Compose

## 启动

在项目根目录启动 MongoDB：

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

打开 <http://127.0.0.1:5173>，进入「资产管理」上传图片，再回到「海报设计」选择素材并生成。接口文档位于 <http://127.0.0.1:8000/docs>。

## 配置

`backend/.env` 保存本地密钥，已被 `.gitignore` 排除，不要提交。关键项：

```dotenv
MONGODB_URI=mongodb://127.0.0.1:27018
MONGODB_DATABASE=social_media
STORAGE_DIR=../local_media
SKILLS_DIR=../.agents/skills

CORE_AI_GATEWAY_BASE_URL=https://core-ai-server.connexup-uat.net/api/gateway/v1
CORE_AI_SERVER_BASE_URL=https://core-ai-server.connexup-uat.net
CORE_AI_API_KEY=replace_me
CORE_AI_PROMPT_AGENT_ID=9a7eb9c3-0d57-4841-83b2-ea9191ff8dd8
CORE_AI_PROMPT_AGENT_TIMEOUT_SECONDS=900
CORE_AI_IMAGE_AGENT_ID=86c3a160-54fa-4344-9bc1-6ac56c1e080c
CORE_AI_IMAGE_AGENT_TIMEOUT_SECONDS=900
CORE_AI_IMAGE_MODEL=gpt-image-2.5-sunburst
CORE_AI_DESIGN_MODEL=gpt-6.1-sol
CORE_AI_IMAGE_QUALITY=high
CORE_AI_IMAGE_TIMEOUT_SECONDS=300
LLM_TIMEOUT_SECONDS=120

IMAGE_HOST_BASE_URL=https://43.139.235.133:5002
IMAGE_HOST_UPLOAD_TOKEN=replace_me
IMAGE_HOST_PUBLIC_BASE_URL=https://43.139.235.133/img
IMAGE_HOST_TIMEOUT_SECONDS=60

# 默认留空，使用 local_media/objects。
COS_SECRET_ID=
COS_SECRET_KEY=
COS_BUCKET=
COS_REGION=
COS_PREFIX=social-media
```

当前默认使用本地对象存储。提示词优化需要配置自建图床；图片仅在第一次需要外部模型读取时上传，返回 URL 会缓存在素材记录中。COS 配置留空即可，仅保留兼容入口。

## 品牌与素材

品牌运行时配置保存在 MongoDB 的 `brands` 集合。资产库中的图片不预设内容、Logo 或参考图角色；同一张图片可以在生成页按位置分别作为内容素材、Logo 或风格参考，角色由任务字段和品牌关联决定。任务创建时会把平台、用途、主色调、Logo 和参考图 asset id 写入 `whole_tasks.brand` 快照，后续品牌修改不会影响旧任务。生成页可单独选择当前风格的 Logo，默认使用第一张，也可选择不使用。

首次导入或重新导入 skill：

```powershell
Set-Location backend
.\.venv\Scripts\python.exe scripts\import_brands.py --skills-dir ..\.agents\skills
```

默认只导入不存在的品牌；覆盖已有品牌加 `--force`，只导入某个品牌加 `--only seejoy`。


将现有喜聚、朵颐规范整理为表单字段（保留素材关联和历史任务）：

```powershell
Set-Location backend
# 默认只预览；执行前自动备份，重复执行跳过已迁移的风格。
.\.venv\Scripts\python.exe scripts\initialize_brand_templates.py
.\.venv\Scripts\python.exe scripts\initialize_brand_templates.py --apply
```

备份写入 `storage/backups/brand-templates/<时间>/brand-templates.ejson`。原规范在风格详情中只读留存。已有结构化风格不会被 skill 的 `--force` 导入覆盖。

将现有素材、风格卡片图片和最近 N 个任务的图片迁移到本地（默认预览，`--apply` 执行）：

```powershell
Set-Location backend
.\.venv\Scripts\python.exe scripts\migrate_media_to_local.py --recent-tasks 10
.\.venv\Scripts\python.exe scripts\migrate_media_to_local.py --recent-tasks 10 --apply
```

运行时对象位于 `local_media/objects/`；便于浏览的归档位于 `local_media/archive/materials/`、`archive/brand-styles/` 和 `archive/recent-tasks/`。迁移报告写入 `local_media/migration-report.json`。

将迁移后的全部素材和最近 10 个任务图片上传到自建图床：

```powershell
Set-Location backend
.\.venv\Scripts\python.exe scripts\upload_local_media_to_image_host.py
.\.venv\Scripts\python.exe scripts\upload_local_media_to_image_host.py --apply
```

图床返回的永久 443 直链会写回素材记录并保存在 `local_media/image-host-report.json`。

## 提示词优化基准

提示词优化基准脚本只覆盖整图流程：

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m scripts.prompt_optimization_loop prepare --run-dir <run-dir>
.\.venv\Scripts\python.exe -m scripts.prompt_optimization_loop optimize-and-generate --run-dir <run-dir>
.\.venv\Scripts\python.exe -m scripts.prompt_optimization_loop score --run-dir <run-dir> --score-file <score.json>
.\.venv\Scripts\python.exe -m scripts.prompt_optimization_loop report --run-dir <run-dir>
```

报告写入对应 run 目录的 `index.html`。

## 测试

### 平台图片 Agent 独立验证

`image-gen-skill/` 是上传到 Core AI 平台的工具版 skill，使用内置 `generate_image`；外部调用脚本仅调用已发布 Agent，不直接调用图片网关。默认 Agent 为 `86c3a160-54fa-4344-9bc1-6ac56c1e080c`，读取 `backend/.env` 中的 `CORE_AI_SERVER_BASE_URL` 和 `CORE_AI_API_KEY`，进程环境变量优先。

在项目根目录运行（将图片路径替换为实际文件）：

```powershell
& 'backend/.venv/Scripts/python.exe' -X utf8 'backend/scripts/call_image_gen_agent.py' --image 'D:/work/poster.png' --image 'D:/work/background.png' --prompt '将图一的背景替换为图二' --dry-run
& 'backend/.venv/Scripts/python.exe' -X utf8 'backend/scripts/call_image_gen_agent.py' --image 'D:/work/poster.png' --image 'D:/work/background.png' --prompt '将图一的背景替换为图二'
```

图片顺序就是 `--image` 顺序。真实调用会消耗平台额度，结果默认保存到 `output/image-gen-skill/script-时间戳/`，包含 `report.json`、`response.md` 和图片。超时后先查看报告里的 run ID，用 `--run-id <ID> --out-dir <新的目录>` 继续查询，避免重复生成。已有输出目录不会被覆盖。图片返回成功与文字、背景、装饰的视觉验收分别判断。

### 项目检查

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest
Set-Location ..\frontend
npm run build
npm run lint
```

默认测试不调用付费模型。真实生图验证会消耗 core-ai 网关额度，建议一次生成加一次自然语言改图。

## 目录

```text
backend/            FastAPI、整图生成与编辑服务
frontend/           React + TypeScript 单页工作台
.agents/skills/     品牌 skill 种子与文档
local_media/        本地运行时对象与素材、风格、任务归档
storage/            备份、日志和实验产物
docker-compose.yml  MongoDB，127.0.0.1:27018
```

旧分层工作流的 `tasks`、`versions` 等历史数据库记录和生成文件不会自动删除，但当前应用不再读取或提供对应接口。
