# AI 视觉设计 Agent

从资产管理选择最多 4 张参考素材，结合品牌风格、发布平台和设计提示词，由 core-ai 图片模型直接生成完整设计图；生成后可继续用自然语言改图，每次成功操作保存为独立版本。

当前只保留“自由设计（整图）”工作流：模型直接输出完整图片，不做图层拆分、文字层规划或 Pillow 合成。

## 主要能力

- 左侧菜单包含「自由设计」「资产管理」「品牌风格」。
- 自由设计支持选择预设品牌、参考图风格或无品牌模式。
- 品牌规范保存在 MongoDB `brands` 集合；`.agents/skills/<brand>/` 仅用于首次导入和人工文档，运行时不读取。
- 品牌风格页面支持卡片列表、详情编辑、指南修改，以及品牌参考图上传、删除和封面设置。
- 资产管理支持上传图片、载入 `素材/` 示例素材、查看已保存素材和删除素材。
- 每次生成支持 1–6 张候选，默认 3 张；候选可在结果区切换，选中的版本继续编辑。
- 发布平台为选填项：选择平台后可指定用途，比例、尺寸、安全区和联系方式规则由平台预设统一提供；不选择时可手动指定画幅比例。
- 当前包含小红书、Instagram、TikTok、Google Business Profile 和 Meta 广告，支持 3:4、4:3、1:1、4:5、9:16、16:9 画布。
- 支持 `gpt-image-2.5-sunburst`、`gemini-3.1-flash-image`、`seedream-5-pro-image-to-image`，默认使用 `gpt-image-2.5-sunburst`。
- 提示词输入框支持 AI 优化，会根据当前品牌、素材、平台规则和商家信息生成可直接用于整图生成的提示词。
- 生成和改图调用 core-ai `/images/generations` 与 `/images/edits`；自然语言改图以选定版本作为源图，不调用旧的图层规划或文字渲染链路。
- 任务、版本、候选和模型输入快照保存在 MongoDB；图片保存在 COS，未配置 COS 时回退到本地 `storage/objects/`。
- 刷新页面可通过 `?whole=<task_id>` 恢复整图任务和当前版本。

## 工作流接口

| 接口 | 用途 |
|---|---|
| `POST /api/whole-image/tasks` | 创建整图任务，返回 202 |
| `GET /api/whole-image/tasks/{id}` | 获取任务、当前版本和模型输入 |
| `POST /api/whole-image/tasks/{id}/edits` | 基于 `source_version_id` 执行自然语言改图 |
| `GET /api/whole-image/tasks/{id}/versions` | 获取版本列表 |
| `POST /api/whole-image/tasks/{id}/versions/{version_id}/select` | 选择继续编辑的版本 |
| `GET /api/whole-image/tasks/{id}/download?version_id=...` | 下载指定版本 |
| `GET /api/whole-image/batches/{batch_id}` | 获取候选批次 |

公共接口：

| 接口 | 用途 |
|---|---|
| `GET /api/assets` | 素材列表，可使用 `?kind=` 过滤 |
| `POST /api/assets` | 上传图片素材 |
| `DELETE /api/assets/{id}` | 删除素材；被整图任务引用时需显式 `force=true` |
| `GET /api/brands` / `GET /api/brands/{id}` | 品牌列表与详情 |
| `PUT /api/brands/{id}` | 更新品牌配置 |
| `POST /api/brands/{id}/references` | 上传品牌参考图，最多 5 张 |
| `DELETE /api/brands/{id}/references/{asset_id}` | 移除品牌参考图 |
| `GET /api/platforms` | 平台、用途、尺寸、安全区和联系方式规则 |
| `POST /api/prompts/optimize` | 优化整图生成提示词 |
| `GET /api/samples` | 导入 `素材/` 示例素材 |
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

打开 <http://127.0.0.1:5173>，进入「资产管理」载入示例素材，再回到「自由设计」选择素材并生成。接口文档位于 <http://127.0.0.1:8000/docs>。

## 配置

`backend/.env` 保存本地密钥，已被 `.gitignore` 排除，不要提交。关键项：

```dotenv
MONGODB_URI=mongodb://127.0.0.1:27018
MONGODB_DATABASE=social_media
STORAGE_DIR=../storage
SAMPLE_DIR=../素材
SKILLS_DIR=../.agents/skills

CORE_AI_GATEWAY_BASE_URL=https://core-ai-server.connexup-uat.net/api/gateway/v1
CORE_AI_SERVER_BASE_URL=https://core-ai-server.connexup-uat.net
CORE_AI_API_KEY=replace_me
CORE_AI_PROMPT_AGENT_ID=9a7eb9c3-0d57-4841-83b2-ea9191ff8dd8
CORE_AI_PROMPT_AGENT_TIMEOUT_SECONDS=190
CORE_AI_IMAGE_MODEL=gpt-image-2.5-sunburst
CORE_AI_DESIGN_MODEL=gpt-6.1-sol
CORE_AI_IMAGE_QUALITY=high
CORE_AI_IMAGE_TIMEOUT_SECONDS=300
LLM_TIMEOUT_SECONDS=120

COS_SECRET_ID=replace_me
COS_SECRET_KEY=replace_me
COS_BUCKET=your-bucket-1250000000
COS_REGION=ap-guangzhou
COS_PREFIX=social-media
```

正式运行建议配置 COS；未配置时图片写入本地对象存储，仅适合开发和离线测试。

## 品牌与素材

品牌运行时配置保存在 MongoDB 的 `brands` 集合。品牌参考图和封面存放在 `assets` 集合与对象存储中，`kind` 为 `brand_reference`。任务创建时会把品牌配置和参考图 asset id 写入 `whole_tasks.brand` 快照，后续品牌修改不会影响旧任务。

首次导入或重新导入 skill：

```powershell
Set-Location backend
.\.venv\Scripts\python.exe scripts\import_brands.py --skills-dir ..\.agents\skills
```

默认只导入不存在的品牌；覆盖已有品牌加 `--force`，只导入某个品牌加 `--only seejoy`。

同步 `素材/` 文件夹会清空并重新导入全部素材：

```powershell
Set-Location backend
.\.venv\Scripts\python.exe scripts\sync_materials.py
```

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
storage/            本地对象存储回退和实验产物
素材/               示例素材
docker-compose.yml  MongoDB，127.0.0.1:27018
```

旧分层工作流的 `tasks`、`versions` 等历史数据库记录和生成文件不会自动删除，但当前应用不再读取或提供对应接口。
