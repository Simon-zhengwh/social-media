# AI 视觉设计 Agent

从资产管理选择最多 6 张素材图，结合品牌风格、发布平台和设计提示词，由 core-ai 图片模型直接生成完整设计图；生成后可继续用自然语言改图，每次成功操作保存为独立版本。

当前只保留“海报设计（整图）”工作流：模型直接输出完整图片，不做图层拆分、文字层规划或 Pillow 合成。

## 主要能力

- 左侧菜单包含「海报设计」「任务中心」「资产管理」「品牌风格」。
- 海报工作台新增「原照替换」：以当前选中海报为底图，初始内容素材默认不勾选；用户选择需要替换的素材并填写具体位置，也可上传、从资产库补充或复用同一原照，每次提交 1–5 项。
- 原照替换结果先展示前后对比，点击「应用为新版本」后才切换当前图；每项替换、来源版本及选中结果均保存。后台等待期间可继续改图；应用旧来源结果时会提示版本差异，应用与正在执行的改图互斥，请求重试不会重复新增版本。
- 刷新工作台或从任务中心打开关联替换任务可恢复记录，查询超时结果和服务重启恢复沿用原替换 run。原照替换只标记已执行的操作，不提供自动像素保真认证；后续普通改图不会继承原照替换标记或自动再次替换。
- 原照替换通常约 30 分钟，本地最多等待 45 分钟。浏览器关闭不影响后台执行；已保存 run ID 的关联任务在服务重启后继续查询，截止时间不重置。等待超时后可「查询已有结果」，不会再次触发 Agent；远端仍可能继续处理。查询时远端仍在运行且尚未到原定截止时间，会恢复自动查询，只有到期后才提示等待超时。
- 原照替换按图片顺序直接发送自然语言要求与 URL；填写「背景」时明确为替换整张摄影底图（包括照片中的主体和场景），保留文字、Logo、笔刷、贴纸、装饰、排版及画布尺寸，与界面说明一致。其他局部替换目标按原文发送，不追加项目执行规范或强制回复格式；原照处理遵循 Agent 自身的 skill。
- 原照替换从 Agent 最终回复中的图片/下载链接或明确标注的成图附件读取唯一最终图，校验与原图精确同尺寸；排除蒙版、预览和对比图，存在歧义时不标记成功。
- 海报设计支持选择预设品牌、参考图风格或无品牌模式。
- 品牌规范保存在 MongoDB `brands` 集合；`.agents/skills/<brand>/` 仅用于首次导入和人工文档，运行时不读取。
- 每套品牌风格必须绑定一个发布平台及平台预设的发布用途；可从现有风格复制出独立平台版本。
- 品牌风格页面支持紧凑基本信息编辑、生成规范折叠编辑、参考图管理，以及最多 6 个主色调管理。颜色统一使用 `#RRGGBB` HEX。品牌 Logo 属于商户通用字段（左侧独立卡片，最多 5 张），全部平台风格共用，不再随单个风格保存。
- 品牌风格使用一个选填的「备注」框填写要求，保留平台、用途、Logo、主色调和参考图。风格定位与原细分模板暂时隐藏，原数据继续保存以便恢复，但不再传给提示词优化或生图模型；规范预览也只包含备注及保留的配置。
- 「通用规范」可在品牌风格页编辑，覆盖文案准确、素材使用、Logo 和可读性。更新应用于新任务；任务保存当时的规范正文和版本，历史任务不变。
- 资产管理支持上传图片、查看已保存素材和删除素材。
- 每次生成支持 1–6 张候选，默认 3 张；候选可在结果区切换，选中的版本继续编辑。
- 通用规范「素材使用」要求输入的菜品、摄影场景及其他内容素材保真：仅允许基于原图进行整体裁切、等比缩放、平移、画面内旋转，以及相对原视角严格小于 30° 的轻微透视变换；禁止 30° 及以上的视角变化、非等比拉伸、重绘、生成式补画或扩图，不得编造原素材没有或被遮挡的物体与细节。提示词优化、生成和整图改图均传入限制，保留数量、形态、颜色、纹理、摆盘、餐具、光影及内部相对位置。这是模型提示约束，不提供像素保真或透视角度的自动认证。
- 发布平台为选填项：选择平台后可指定用途，比例、尺寸和安全区规则由平台预设统一提供；不选择时可手动指定画幅比例。
- 当前包含小红书、Instagram、TikTok、Google Business Profile 和 Meta 广告，支持 3:4、4:3、1:1、4:5、9:16、16:9 画布。
- 小红书平台预设禁止出现地址、电话号码和二维码；用户文案包含相关内容时，生成提示词会要求省略。
- 生成页「图片模型」提供 `gpt-image-2.5-sunburst`（默认）、`seedream-5-pro-image-to-image`、`seedream-5.5-image-to-image` 与 `minimax-h3-image`；`gemini-3.1-flash-image` 接口仍接受（兼容历史任务），页面不再展示。选择后按任务保存，改图沿用该任务的模型。
- 为临时验证 MiniMax，另提供 `minimax-h3-image` 模型（不含高速版）：后端直连 MiniMax H3 Image（`/v1/images/generations`、`/v1/images/edits`），不经过 core-ai。参考图统一以 base64 data URI 内联，尺寸自动收敛到 MiniMax 的 655,360–20,000,000 像素区间；文生图默认关闭 prompt 增强以保留项目保真规则，改图不支持蒙版。core-ai 接入同一模型后删除 `app/services/minimax_image.py`、`app/services/image_provider.py` 与 `MINIMAX_*` 配置即可回退到纯网关。
- 提示词输入框支持 AI 优化，会根据当前品牌、素材、平台规则和商家信息生成可直接用于整图生成的提示词。
- 优化 Agent 使用 `.agents/skills/image-gen-prompt/`：从官方 `imagegen` skill 抽取提示词编写原则、可选字段和示例，按任务需要组织提示词。
- 图片默认保存在项目内 `local_media/objects/`；提示词优化按需把本地图片上传到自建图床，使用永久 443 公网直链供外部模型读取。
- 接口返回 `{"prompt":"..."}`，保留非空和长度校验；不要求固定七段、独立事实数组或案例专用的设计规则。优化后的提示词直接交给生图模型。
- 生成调用 core-ai `/images/generations`；自然语言改图只走 `/images/edits` 直接调用模型，单次返回一张结果并自动保存为新版本，不再并行触发 image-editor Agent。
- 改图发送本轮修改要求、原始内容素材（含选定 Logo）和选中版本的生成图；选中图明确作为编辑底图，原始素材仅用于核对主体细节，避免重新构建海报。仅改文字时要求只修改指定文字及替换所需的紧邻背景。
- 「原照替换」仍使用 `CORE_AI_BACKGROUND_REPLACE_AGENT_ID` 指定的 image-editor Agent；该 Agent 与「海报修改」独立菜单共用产物存储：HTML/ZIP 保存在对象存储 `artifacts/` 前缀，元数据写入 MongoDB `whole_artifacts`，工作台内可在沙箱隔离的 iframe 内嵌预览 HTML 并导出 PNG，历史版本没有产物时继续只显示图片。
- 原照替换 Agent 单次通常耗时10–30分钟，默认超时1800秒；它不使用 attachments，而是把公开图片 URL 拼进 Agent `input`，通过 `trigger + get` 获取产物。返回图片时优先接收最终回复中明确引用的图片，避免把中间步骤和占位图当成最终结果；最终回复没有图片链接时，才兼容读取图片附件列表。
- 任务、版本、候选和模型输入快照保存在 MongoDB；图片以本地对象存储为主，保存到项目内 `local_media/objects/`。
- 侧边栏「任务中心」按候选批次聚合展示生成记录，可筛选状态、下载成品、页内重试失败项，并点击任务回到工作台继续编辑；「原照替换」筛选只展示关联海报的替换记录，点击后回到对应海报。
- 一次改图保存为一个版本，只包含接口直出的一张结果，完成后可直接继续修改；原照替换结果应用后另存为一个独立版本。
- 改图输出会归一化回源图的精确像素尺寸和比例。模型返回的比例不一致时，完整等比缩放后居中留边，不再裁切图片；透明图层留透明边，成图留白边。页面结果预览完整显示图片。
- 刷新页面可通过 `?whole=<task_id>` 恢复整图任务和当前版本。
- 任务中心使用 `?view=tasks`；打开任务后会切换到 `?view=whole&whole=<task_id>`（整图页省略 `view` 参数）。

### 本机素材筛选（SMB 初筛）

- 侧边栏「素材筛选」（`?view=material-filters`）只在本机开发模式开放：后端要求 `AM_BYPASS_LOCAL=1` 且来源为回环地址，页面通过 `features.material_filters` 决定是否显示；非本机模式即使直接请求接口也返回 403。
- 顶部每个 SMB 目录一个 TAB。通过「＋添加文件夹」输入 `\服务器\共享\目录`、`file://服务器/共享/目录` 或 `smb://服务器/共享/目录`；等价地址（含中文、空格和 URL 编码）只保留一个 TAB。使用当前 Windows 已连接的共享，不在页面里配置账号密码，也不自动挂载。
- **SMB 严格只读**：只枚举、`stat` 和以 `rb` 打开图片；副本、缩略图、临时文件和导出全部写本机。添加文件夹时只列一次目录，不会用创建文件的方式探测权限。
- 每个 TAB 内可保存多套独立方案（自然语言条件、子目录开关、本机输出目录、模型快照）。修改判定标准请「复制为新方案」，旧方案结果保留，不会被原地改写。
- 「扫描并筛选」是手动增量任务：按相对路径＋原文件名定位；大小和修改时间未变直接跳过，变化后计算 SHA-256，内容一致复用原结论，实际内容变化才重新调用模型。关闭页面不会中断后端任务；服务重启后遗留任务标记为中断，由用户点「继续中断任务」。
- 结果分通过、不通过、待复核和请求失败。任一条明确违反即不通过；没有明确违反但有不确定项则待复核；待复核需人工确认后才复制，复制前会重新核对源文件哈希。技术失败与内容不通过分开记录，下次运行自动重试失败项。
- 通过的原图按「中文名称＋稳定短编号.原扩展名」复制到本机 `<STORAGE_DIR>/material-filters/<merchant_id>/<folder_id>/<scheme_id>/passed/`；缩略图在同目录 `previews/`。输出目录必须是本地固定磁盘，UNC、映射网络盘、设备路径和目录链接/junction 会在任何写入前被拒绝。
- 名称和标签可人工修改，只更新数据库，不改磁盘文件名、不重新调用模型。导出清单为 UTF-8 CSV，包含名称、分类、标签、状态、原文件名、来源相对路径、内容哈希和本机副本名。
- 模型只判定硬性条件，最终挑选是第二层人工操作：结果卡片上的「☆ 入选」把要用的照片收进「已入选」分组（再次点击取消），与通过/不通过、待复核互不影响。连拍近似片可切到「按原文件名」排序，相邻帧会排在一起方便逐张比较。
- 结果支持分页浏览：底部显示「已显示 N / 总数」，可「再显示 60 张」或「全部显示」；单次最多 500 张，更多时用状态或搜索缩小范围。
- 文件夹 TAB 可删除：文件夹标题旁的 `🗑` 会移除该 TAB 及其方案、运行记录与筛选结果（数据库记录），**SMB 原图始终不动**；本机 `passed/` 副本和缩略图默认保留，勾选「同时删除本机的通过副本和缩略图」才会清理。有扫描任务运行时拒绝删除。

### 把筛选结果同步到 UAT 资产库

模型只负责硬性条件，最终挑图在「☆ 入选」。入选后的照片有两种出口：

1. **脚本直接上传到 UAT 资产库（推荐）**。脚本只读本机副本，逐张调用已部署的 `/api/merchants/{id}/assets`，上传后把 UAT asset id 写回本地记录（页面上出现「已同步」标记）。服务端按 SHA-256 去重，重复执行不会产生重复素材。

```powershell
Set-Location backend
# 先看会传什么
.\.venv\Scripts\python.exe -X utf8 scripts\sync_picked_to_uat.py --scheme 排除空座位和蟹腿 --orca-auto --dry-run
# 正式上传（--orca-auto 复用 Orca 里已登录的 UAT 标签页会话）
.\.venv\Scripts\python.exe -X utf8 scripts\sync_picked_to_uat.py --scheme 排除空座位和蟹腿 --orca-auto --report ..\storage\diagnostics\uat-sync.csv
```

没有 Orca 会话时，用 `AM_SESSION_COOKIE="AMSessionId=..."` 或 `--cookie` 传浏览器的 AM 会话；`--limit N` 先试几张、`--all-passed` 传全部已复制副本、`--force` 忽略已同步标记重传。当前 UAT 版本没有 `/assets/{id}/tags` 接口（返回 404），标签会跳过并记录在 `uat_tag_error`，上传不受影响。
- 上传到 UAT 时文件名只保留 AI 中文名（去掉本地副本的 `__短ID`），库里撞名时依次加 `-2`、`-3`；脚本启动时先读一次资产库已有文件名，因此多次运行的编号保持稳定。扩展名沿用本机副本（`.jpg`/`.heic` 等），本地副本仍带短号用于回溯。

2. **导出到本机目录再手工上传**：结果页「⋯ → 导出已入选到本机」会把入选原图复制到 `<方案目录>/exports/picked-<时间>/`，并生成 `manifest.csv`（文件、名称、原文件名、分类、标签、哈希、评估 ID），可拖到 UAT 资产页逐张上传（该页一次只能选一张，数量多时建议用脚本）。

未配置 UAT 会话或上传失败时，本机记录保留 `uat_sync_error`，重跑即可继续；上传不会改动 SMB 原图，也不会重新调用模型。
- 已知局限：普通增量扫描只比较大小和修改时间，不保证发现被刻意保留元数据的内容变化；需要强制读图时使用结果卡片的「重新检查」。第一版不包含定时扫描、远程采集、相似去重和自动清理 SMB 原图。

## 工作流接口

| 接口 | 用途 |
|---|---|
| `POST /api/whole-image/tasks` | 创建整图任务，返回 202 |
| `GET /api/whole-image/tasks?status=...&limit=...&cursor=...` | 按候选批次获取任务列表 |
| `GET /api/whole-image/tasks/{id}` | 获取任务、当前版本和模型输入 |
| `POST /api/whole-image/tasks/{id}/retry` | 重试失败的生成或改图任务，返回 202 |
| `POST /api/whole-image/tasks/{id}/edits` | 基于 `source_version_id` 执行自然语言改图 |
| `POST /api/whole-image/tasks/{id}/original-replacements` | 选择来源版本、结果和 1–5 项原照替换目标，返回 202 |
| `GET /api/whole-image/tasks/{id}/original-replacements` | 获取当前海报的关联替换记录 |
| `POST /api/whole-image/tasks/{id}/original-replacements/{replacement_id}/apply` | 对比确认后应用为新版本，校验当前版本与结果标识，重复请求不重复应用 |
| `POST /api/whole-image/tasks/{id}/original-replacements/{replacement_id}/refresh-result` | 查询关联替换任务原 run 的已有结果，不重新触发 |
| `GET /api/whole-image/tasks/{id}/original-replacements/{replacement_id}/download` | 下载关联原照替换的最终 PNG |
| `GET /api/whole-image/tasks/{id}/versions` | 获取版本列表 |
| `POST /api/whole-image/tasks/{id}/versions/{version_id}/select` | 选择继续编辑的版本 |
| `GET /api/whole-image/tasks/{id}/download?version_id=...` | 下载指定版本 |
| `GET /api/whole-image/artifacts/{artifact_id}/preview` |内嵌预览 Agent返回的 HTML编辑器（sandbox隔离） |
| `GET /api/whole-image/artifacts/{artifact_id}/download` |下载 Agent返回的 HTML或结果 ZIP |
| `GET /api/whole-image/batches/{batch_id}` | 获取候选批次 |
| `POST /api/whole-image/tasks/{id}/versions/{version_id}/outputs/{output_id}/select` | 选择下一轮编辑源图 |
| `GET /api/task-center?status=...&kind=...&limit=...&cursor=...` | 任务中心列表，类型支持 `all`、`generate`、`original_replace` |

公共接口：

| 接口 | 用途 |
|---|---|
| `GET /api/assets` | 获取资产库中的全部上传图片 |
| `POST /api/assets` | 上传图片素材，上传即进入资产库 |
| `DELETE /api/assets/{id}` | 删除素材；被整图任务引用时需显式 `force=true` |
| `GET /api/brands` / `GET /api/brands/{id}` | 品牌列表与详情 |
| `PUT /api/brands/{id}` | 更新品牌配置 |
| `POST /api/brands` | 新建结构化品牌风格 |
| `POST /api/brands/preview` | 预览未保存的风格、通用规范及平台规则 |
| `GET /api/brand-rules` / `PUT /api/brand-rules` | 查看和更新通用规范，更新需携带当前版本 |
| `POST /api/brands/{id}/duplicate` | 复制为另一个平台/用途的独立品牌风格 |
| `POST /api/brands/{id}/references` | 上传品牌参考图，最多 5 张 |
| `DELETE /api/brands/{id}/references/{asset_id}` | 移除品牌参考图 |
| `GET /api/brand-logos` | 获取商户级品牌 Logo（全部平台风格共用） |
| `POST /api/brand-logos` | 批量上传商户级品牌 Logo，最多 5 张 |
| `DELETE /api/brand-logos/{asset_id}` | 从商户级 Logo 集合移除 |
| `GET /api/platforms` | 平台、用途、尺寸和安全区规则 |
| `POST /api/prompts/optimize` | 优化整图生成提示词 |
| `GET /api/health` | MongoDB、对象存储和模型配置健康检查 |

## 环境

### UAT 集成

UAT 使用 `/api/merchants/{merchant_id}/...`，页面需携带 AM Console 的商户上下文和会话。原照替换、Agent 测试和 HTML/ZIP 产物接口同样受商户授权、CSRF 与维护模式保护；商户资料从 AM 读取，四项中文摘要用于界面、提示词优化和任务快照。

UAT 图片及 HTML/ZIP 产物通过 AM 媒体服务保存到 Azure，逻辑 `artifacts/` 对应 AM 的 `OUTPUTS`，继续保留原扩展名。发布时需同步 ConfigMap 中的 `CORE_AI_IMAGE_AGENT_ID=5a162c34-f333-453f-9835-de6a4f96e3fb` 和 `CORE_AI_IMAGE_AGENT_TIMEOUT_SECONDS=1800`，以免旧环境配置覆盖新默认值。

Agent 测试页的固定素材不随 Git 或镜像发布；未准备 `agent-test-cases/manifest.json` 的 UAT 环境会返回 `benchmark_missing`，不影响海报生成和编辑。测试报告缓存丢失时按已保存 run ID 重新读取结果，不重新触发付费任务。

- Node.js 22.x
- Python 3.12
- Docker Desktop + Docker Compose

## 启动

### 本地开发模式（推荐）

在当前项目中开发和调试，复用已有 demo 的本机 MongoDB 与图片，不必把业务代码在两个项目之间来回迁移。
本地模式跳过 AM 登录，商户资料、素材和历史任务读取本机数据库；图片及 HTML/ZIP 使用本地磁盘，包括后台任务恢复。
生图、改图、原照替换和提示词优化仍调用真实 Core AI；提示词优化读取本地图片时仍需配置图床，实际模型调用会消耗额度。

首次使用，按下面的后端、前端说明安装依赖，然后创建独立配置：

```powershell
Copy-Item backend/.env.local.example backend/.env.local
# 编辑 backend/.env.local，填写已有数据库名称、数据所属商户 ID、模型和图床凭据。
# 过渡期可设置 STORAGE_DIR=D:/ai_project/social-media/local_media，保留 demo 的现有文件。
```

`backend/.env.local` 不进入 Git，且仅由本地启动入口显式加载。它替代 `backend/.env`，不会合并读取其中的线上凭据；进程环境变量仍然优先。
部署时沿用原启动命令和环境变量，不设置 `SOCIAL_MEDIA_ENV_FILE`，不会自动读取本地配置。保持部署环境 `AM_BYPASS_LOCAL=0`。
本地入口仅允许连接本机 MongoDB；它不会自动创建、清空、迁移或导入数据库。

在项目根目录执行：

```powershell
.\start-local.ps1 -Check  # 只读检查配置、商户数据和文件完整性，不启动服务或调用模型。
.\start-local.ps1         # 同时启动前后端，打开 http://127.0.0.1:5173。
```

启动前请先结束 demo 中的运行任务并停止旧后端。两个目录共用数据库时，不要同时运行两个后端：应用启动会处理已有的运行中任务。
入口检测到运行中任务或端口占用时会停止启动，不会替你终止已有服务。按 `Ctrl+C` 会停止本次启动的前后端。
若上次后端异常退出或在任务执行期间停止，确认旧后端已退出后，可用 `./start-local.ps1 -Recover`（Python 入口为 `--recover`）处理遗留任务。
该选项允许现有启动恢复逻辑运行：普通生图和改图标记中断，已有 Agent run 沿用原流程恢复，不表示所有任务都会自动重新生成。`-Check -Recover` 仍然只读。
前端使用 Vite 热更新；修改 Python 后端后需停止并重新启动，避免热重载打断生成任务。
历史文件缺失会列出数量，不会删除数据库记录；缺少固定 Agent 测试素材只影响对应测试页。

需要其他端口时，可执行 `./start-local.ps1 -BackendPort 8001 -FrontendPort 5174`，前端代理与本地请求来源会一起调整。
如果 PowerShell 的执行策略阻止脚本，可直接使用后端虚拟环境：

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -X utf8 -m scripts.local_dev --check
.\.venv\Scripts\python.exe -X utf8 -m scripts.local_dev
```

后续停止使用 demo 后，可以将 `local_media` 复制到当前项目并把 `STORAGE_DIR` 改为 `../local_media`，数据库仍可沿用原库。
素材、品牌和任务修改会写入这份共用的本机数据库；图片上传、生成与删除也会操作配置指定的图片目录。

### 手动启动和依赖准备

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
CORE_AI_IMAGE_AGENT_ID=5a162c34-f333-453f-9835-de6a4f96e3fb
CORE_AI_IMAGE_AGENT_TIMEOUT_SECONDS=1800
CORE_AI_BACKGROUND_REPLACE_AGENT_ID=5a162c34-f333-453f-9835-de6a4f96e3fb
CORE_AI_BACKGROUND_REPLACE_AGENT_TIMEOUT_SECONDS=2700
CORE_AI_IMAGE_MODEL=gpt-image-2.5-sunburst
CORE_AI_DESIGN_MODEL=gpt-6.1-sol
CORE_AI_IMAGE_QUALITY=high
CORE_AI_IMAGE_TIMEOUT_SECONDS=300
LLM_TIMEOUT_SECONDS=120

# 临时直连 MiniMax 图片模型（core-ai 暂未接入）。
MINIMAX_IMAGE_BASE_URL=https://api.minimax.cn/v1
MINIMAX_IMAGE_API_KEY=replace_me
MINIMAX_IMAGE_MODEL=MiniMax-H3-Image
MINIMAX_IMAGE_TIMEOUT_SECONDS=600

IMAGE_HOST_BASE_URL=https://43.139.235.133:5002
IMAGE_HOST_UPLOAD_TOKEN=replace_me
IMAGE_HOST_PUBLIC_BASE_URL=https://43.139.235.133/img
IMAGE_HOST_TIMEOUT_SECONDS=60

# AM Console 会话来源。本地直连时改成 http://127.0.0.1:5173。
SOCIAL_MEDIA_ORIGIN=https://social-media.connexup-uat.net
SOCIAL_MEDIA_URL=https://social-media.connexup-uat.net

#本地直连开发：跳过 AM Console 会话校验，固定打开一个商户。默认关闭。
AM_BYPASS_LOCAL=0
AM_BYPASS_MERCHANT_ID=

# 默认留空，使用 local_media/objects。
COS_SECRET_ID=
COS_SECRET_KEY=
COS_BUCKET=
COS_REGION=
COS_PREFIX=social-media
```

不经过 AM Console 直接开发时，把 `SOCIAL_MEDIA_ORIGIN` 设为 `http://127.0.0.1:5173`，并设置 `AM_BYPASS_LOCAL=1` 与 `AM_BYPASS_MERCHANT_ID=<商户 ID>`。此时跳过 AM 会话校验：素材库只显示本地上传，媒体存储落到本地磁盘，商户资料读取 MongoDB `merchant_profiles` 中该商户已导入的记录。打开 <http://127.0.0.1:5173>会自动进入该商户工作区。该开关只在本机来源下生效，部署环境即使误设也不会启用。

当前默认使用本地对象存储。提示词优化需要配置自建图床；图片仅在第一次需要外部模型读取时上传，返回 URL 会缓存在素材记录中。COS 配置留空即可，仅保留兼容入口。

`CORE_AI_BACKGROUND_REPLACE_AGENT_ID` 与对应超时配置继续用于海报工作台内的原照替换，沿用原环境变量名；关联记录继续使用原集合以保留历史。已废弃的独立修改、背景替换任务与生成文件保留，但不再提供接口、任务中心入口或启动恢复。

## 品牌与素材

品牌风格详情的「商户特点」只展示实际传给模型背景的四项中文摘要：商户定位、核心卖点、目标客群、表达语气。品牌详情、提示词优化 Agent、生图和整图改图复用同一份后端筛选结果，并随任务品牌快照保存；本次要求和备注优先，平台限制继续生效，摘要作为背景参考，不默认全部写入海报。原始资料和未选入摘要的字段不传入模型。商户资料独立保存在 MongoDB `merchant_profiles`，通过品牌文档的 `merchant_id` 显式关联；复制平台风格时保留关联。未关联、资料缺失或摘要为空时隐藏商户面板，不根据名称推测资料。品牌列表和风格选择卡片使用备注，不再显示停用的旧风格摘要。

喜聚资料种子位于 `backend/data/merchant_profiles/seejoy.json`，保留用户提供的原始 JSON、中文整理、来源字段及原文，未独立核验。原始资料、来源、其他未选入摘要的字段继续保存在数据库，不在页面展示或传入模型；品牌详情接口保留旧字段以兼容现有客户端，并增加 `prompt_sections` 供页面展示、提示词优化以及生图/改图共同使用。

导入前预览，确认显式关联的风格 ID 后加 `--apply`（不会覆盖不同的已有商户资料）：

```powershell
Set-Location backend
.\.venv\Scripts\python.exe scripts/import_merchant_profile.py data/merchant_profiles/seejoy.json --brand-id seejoy
.\.venv\Scripts\python.exe scripts/import_merchant_profile.py data/merchant_profiles/seejoy.json --brand-id seejoy --apply
```

品牌运行时配置保存在 MongoDB 的 `brands` 集合，商户级 Logo 保存在 `merchant_logos`。资产库中的图片不预设内容、Logo 或参考图角色；同一张图片可以在生成页按位置分别作为内容素材、Logo 或风格参考，角色由任务字段和品牌关联决定。任务创建时会把平台、用途、主色调、Logo 和参考图 asset id 写入 `whole_tasks.brand` 快照，后续品牌修改不会影响旧任务。生成页默认引用商户 Logo 的第一张，也可为本次海报换用其他素材或选择不使用；生成页上传的 Logo 仅覆盖本次任务并进入资产库。生成页素材图可从资产库多选，也可直接上传，上传的图片同样进入资产库。

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

### image-editor 固定基准页

在侧边栏打开「Agent 测试」，或访问 `http://127.0.0.1:5173/?view=agent-tests`。内置文字、炸鸡背景、南瓜、夫妻肺片和牛肉元素五例；原始提示词固定，原照字节不做处理，通过独立 `attachments` 传给 Agent。支持单例、全量并发（同一批次最多 5 个独立 run）、历史记录、运行过程和产物下载。图片直接预览，已提交的HTML可在隔离iframe内操作与下载PNG，也可放大查看或下载原文件。Trace 入口为 Core AI 的 `/traces/{traceId}` 页面；本地展示的运行详情来自 `/api/runs/{runId}`，不冒充完整 Trace。

历史批次展示全部记录，不再仅显示最近30条。选中批次后可点击「隐藏此批次」，隐藏状态保存在后端；勾选「显示已隐藏」可查找并「恢复显示」。隐藏仅影响默认列表，不改变运行状态、Trace 或产物。

本地基准素材位于 `local_media/agent-test-cases/`，首轮远端上传后按图片 SHA 与服务地址缓存 URL；后续测试无需重新选择或上传图片。新环境可用已有回归 manifest 初始化一次：

```powershell
.\backend\.venv\Scripts\python.exe backend/scripts/prepare_agent_test_cases.py --manifest <已有五例manifest.json> --out local_media/agent-test-cases
```

`CORE_AI_TEST_AGENT_ID` 默认 `5a162c34-f333-453f-9835-de6a4f96e3fb`，`CORE_AI_TEST_TIMEOUT_SECONDS` 默认1800（30分钟，超时直接标记失败且不再轮询迟到结果）；认证沿用服务端 `CORE_AI_API_KEY`。运行记录保存在 MongoDB，完整运行详情保存于 `local_media/agent-tests/`。刷新和继续查询复用已保存的runId，不重新触发；提交结果未知时会停止该批次，需先在平台确认。此页只在点击运行时调用模型，默认测试不调用付费模型。

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
