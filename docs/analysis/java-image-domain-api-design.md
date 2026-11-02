# Java 图片生成：Domain 与接口设计

创建日期：2026-10-28；更新日期：2026-10-29。状态：已合并本次讨论结论，供整体审阅；不是实施计划，尚未修改业务代码。

本文是唯一设计稿，已合并前期代码分析与本次会话确认的业务约定。后续 domain、接口和实现计划以本文为准，不再依赖分散的讨论记录。

**审阅顺序：** 本轮审核接口时先看第 12 节：先列 AM Console Java 接口，再逐个展开 Request/Response，最后展开 View 和枚举；第 13 节以相同顺序列 Integration 内部契约。第 11 节保留业务规则和 JSON 示例。已确认业务结论见第 1.3 节，domain 见第 2–4 节，执行与复用边界见第 5、7、8、10 节。本稿不等于已实现或已批准开发。

**字段命名规范（最新确认）：** Request/Response/View 的 Java 字段按正式项目使用 camelCase，通过 `@Property(name = "snake_case")` 映射 JSON，例如 merchantId 对应 merchant_id、executionType 对应 execution_type。类名和枚举类型名使用 PascalCase，方法名使用 camelCase。先前关于接口 DTO Java 字段也使用 snake_case 的约定由本次选择替代；本轮不调整第 2–4 节既有 domain 结构与 Mongo 字段定义。

**当前整体 domain 快照：**

```text
RestaurantBrandStyle
├─ merchant_id
├─ platform / placement
├─ logos / references
└─ style: Style

ImageDesign
├─ merchant_id
├─ candidates: []
└─ created_by / created_time / updated_time

ImageVersion（一行代表 MODEL 或 AGENT 的一种执行结果）
├─ design_id / candidate_id
├─ version / execution_type
├─ source_id
├─ input: ImageInput
├─ output: ImageOutput
│  └─ files: List<ImageOutputFile>（file_key / is_transparent）
└─ status / run_id / error
```

一个逻辑版本由一个或两个 ImageVersion 记录组成，例如 `version=2, execution_type=MODEL` 和 `version=2, execution_type=AGENT`。V2 不是一张记录，而是这两条记录共同组成的逻辑版本。
**Domain 审阅说明：** 第 2–4 节集中记录当前字段定义，第 3.1 节解释 ImageDesign / ImageVersion 分开存储的原因，第 4.2 节展开输入输出及辅助值对象。三个独立 collection 和已确认的业务约定保持不变，具体字段与接口供本次整体审阅，不将文档整理视为已经批准实现。字段表中的“必填”指持久化要求，ID、操作人和时间由后端产生，不要求前端全部传入。

## 1. 业务边界与核心结论

命名边界：本功能使用“图片”命名，为后续视频功能留出独立演进空间。建议用 ImageDesign、ImageVersion，以及内嵌 ImageCandidate / ImageInput / ImageReference / ImageOutput / ImageOutputFile；相关服务和接口统一使用 Image / image 前缀。RestaurantBrandStyle 保持原名。候选、版本及两条改图链路的关系不变；不提前加入视频字段或通用媒体父 domain，视频设计与复用边界后续讨论。

仍然只需要三个核心 collection：`restaurant_brand_style`、`image_designs`、`image_versions`。后两个名称暂为建议。

范围与简化约定：

- ImageDesign 内嵌有界 candidates 列表，保留每张候选的独立身份和版本历史，不单独建 candidate collection。
- 不持久化虚构进度百分比、重复的阶段状态、候选数量统计、图片 URL、聊天消息或完整商家资料副本。
- 暂不加入 place_id、Task 关联、素材入库、分享名单、审批、运行锁或数据迁移字段。
- 本期只选 merchant，domain 和接口不增加 place_id。门店地址等具体内容由 AM 在提示词填写，不自动选择某家门店补入。
- 现阶段不考虑多人同时选择不同版本的场景，不扩展选择同步、冲突处理或协作字段；工作台选择是否持久化不纳入本次业务收敛。

需要特别说明两点：

- 提示词优化标记会影响生成时的处理，因此保留在生成请求模型中；domain 只保存最终实际使用的 prompt。失败 V1 手动重新生成时复用已保存的最终输入，不重新优化或按最新风格重组 prompt。
- file-service 的 download 接口返回 base64，不是下载 URL；图片对前端的下载响应需要由 BFF/CDN 或文件响应组织，不能直接透传该接口。

### 1.1 系统边界

```text
AM Console
  → ai-marketing-integration-service
    → MongoDB：RestaurantBrandStyle / ImageDesign / ImageVersion
    → product-service：MediaLibrary
    → file-service：图片文件
    → Core AI：MODEL / AGENT 执行
```

- AM Console 负责页面、当前会话、AJAX 接口及跨服务调用。
- `ai-marketing-integration-service` 负责图片业务、MongoDB、生成编排、状态和结果。
- Media Library 的权威数据在 product-service；本功能复用素材查询，并提供不入素材库的 social-media 图片上传，不创建、更新或删除素材库记录。
- social-media 上传由 AM Console 调用 file-service 保存文件，不经过 product-service 的素材入库流程；文件可持续引用，不增加自动过期、清理或提交后转存流程。
- 生成结果写入 file-service，不进入 Media Library。
- 本功能独立于营销 Task，不添加 `task_id`，不写入 TaskOutput 或 MerchantArtifact。
- 不迁移 demo 的素材、风格、历史图片和版本文件；正式环境重新配置风格。

### 1.2 已有数据与复用边界

| 已有对象 | 处理方式 |
|---|---|
| `RestaurantBrandProfile` | 复用既有关联与查询能力；用户确认 merchant 存在时品牌资料一定存在。选择无品牌风格时不自动将其资料注入生成上下文 |
| `MediaLibrary` | 直接读取图片素材；social-media 上传不创建素材库记录，新图片 domain 不保存 media library ID |
| `Task / TaskOutput` | 不接入；图片功能不依赖待办、指派、审核或提交 |
| `MerchantArtifact` | 不作为图片输出存储 |
| `file-service` | 保存输入文件解析结果和所有生成图片；不保存临时 URL 作为唯一依据 |
| Core AI 客户端 | 复用 Agent 能力；图片模型客户端仍需补齐或适配 |

### 1.3 已确认的核心决定

- 新 collection 只有三个：`restaurant_brand_style`、`image_designs`、`image_versions`。
- 一次创作生成 1–6 个候选；候选身份内嵌在 `ImageDesign`。
- 一个逻辑版本可对应一条或两条 `ImageVersion` 记录，由 `execution_type` 区分 MODEL / AGENT。
- 同一候选内 `version` 递增；唯一约束为 `design_id + candidate_id + version + execution_type`。
- 修改来源只保存 `source_id`，指向来源 `ImageVersion.id`。
- 一次执行只有一张不透明成品；透明图与成品成对返回时固定为一张透明图、一张不透明图。后续修改直接取 source_id 对应记录中唯一的 is_transparent=false 文件，不增加文件选择参数（已确认）。
- `ImageInput` 只保存 `prompt`、`images`、`brand_style_id`。
- 首次直接改素材时，第一张图片固定为修改底图（EDIT_SOURCE），其余为本轮辅助参考（EDIT_REFERENCE）；后端在 Agent 输入中明确图片用途，不让 Agent 自行猜测底图。source_id 为空，底图直接记录为 file_key（本轮补充确认）。
- 后续每轮修改保留最多 5 张附加参考图，修改请求使用可选的 reference_file_keys；两条链路均接收，分别记入 input.images 的 EDIT_REFERENCE。本轮参考文件不自动再次附带到下一轮，不新增 collection（已确认）。
- 返回图片的比例明显错误时，将对应的 MODEL 或 AGENT 执行记录判为 FAILED；另一条链路、其他候选及已有版本不受影响，不通过补边等处理把明显错误的画幅视为成功，也不自动重试（已确认）。
- 超时即失败：模型调用或 Agent 执行等待超时，统一将对应 ImageVersion 标记为 FAILED，error 说明超时；不引入 UNKNOWN 状态。首次 V1 超时后可按既有规则手动重新生成同一 V1，不自动重试，迟到结果不能覆盖失败终态或重新生成的结果（本轮补充确认）。
- 图片模型有且只有一个，先在后端代码中 hardcode；首次生成及后续 MODEL 改图使用同一个固定模型，不提供模型选择、切换或请求参数，不新增模型持久化字段（已确认）。
- 通用规范先由后端统一维护，不提供 AM 页面编辑或独立管理接口，不新增通用规范 collection（已确认）。
- 选择无品牌风格时采用简单模式：提示词优化和首次生图不自动注入 RestaurantBrandProfile 的商家名称、菜系、定位等事实，由 AM 在提示词中填写所需内容；merchant_id 继续用于归属及权限，通用规范和已选平台规则仍适用（已确认）。
- 图片引用统一保存 `file_key`，不在新 domain 中保存 `media_library_id`。
- 新增 `POST /ajax/media-library/social-media/upload`，文件保存到 static container 内的 `{azure.basePath}/media-library/{merchant_id}/social-media/{uuid}.png`；仅不进入素材库，不设计自动删除或到期转存（已确认）。
- `ImageOutput.files` 使用 `List<ImageOutputFile>`；每项为内嵌对象，仅保存 `file_key` 和 `is_transparent`（已确认）。
- Agent 返回的透明 PNG 和完整成品图固定各一张，均保留；`is_transparent=true` 的输出仅供查看，不提供产品下载入口，也不能作为后续改图来源（已确认）。
- Agent 后续改图保留后端追加的保护规则：维持来源图画幅、尽量保留未要求修改的内容，附加图片仅作本轮参考。实际采用的规则和用户修改要求一起保存到 input.prompt，图片按发送顺序保存到 input.images（本轮补充确认）。
- 首次 V1 失败时显示失败，并提供“重新生成”按钮；用户点击后复用同一条 ImageVersion（id 不变），仍执行原候选的 V1，不创建新 Design 或 V2。只保留最近一次状态、错误和输出，不保存尝试历史，不增加 attempt 字段或执行历史表。不点击则保持失败，不自动重试。后续修改失败仍保留原版本，重新提交修改创建下一版本（本轮补充确认）。
- 当前不提供图片历史删除。
- 多人同时选择不同版本的场景暂不考虑。
- 正式功能按“图片”命名；视频另行设计。

## 2. RestaurantBrandStyle：视觉风格

该 collection 与 RestaurantBrandProfile 的关联已确认，其余字段按现有 demo 需求细化，不复制品牌身份和门店资料。用户明确 merchant 存在就一定有对应 RestaurantBrandProfile，分析按此前提进行，不增加品牌资料缺失时的生成分支或资料补建流程。

| Java 字段 | 建议类型 | 含义/约束 |
|---|---|---|
| id | String | 服务端生成 |
| merchant_id | String | 必填，关联 RestaurantBrandProfile.merchantId，创建后不跨商家移动 |
| name | String | 必填，风格名称；不增加名称唯一约束 |
| summary | String | 保留风格定位，建议沿用 demo 必填规则 |
| platform | Platform | 必填，Java 枚举；枚举值对应后端预设，不使用界面显示名称 |
| placement | Placement | 必填，Java 枚举；必须与 platform 组成合法用途组合 |
| colors | List<String> | 默认空，最多 6 个 #RRGGBB；统一大写 |
| logos | List<String> | 默认空，最多 5 个 file_key；去重并校验为同商家图片素材 |
| references | List<String> | 默认空，最多 5 个 file_key；去重并校验为同商家图片素材 |
| style | Style | 必填，内嵌七项文本 |
| created_by | String | 必填，创建人登录用户 ID |
| updated_by | String | 必填，最近修改人登录用户 ID；创建时同 created_by |
| created_time | ZonedDateTime | 必填，服务端创建时间 |
| updated_time | ZonedDateTime | 必填，服务端最近修改时间；创建时同 created_time |

七项 style 内容：typography、subject_style、background、layout、decoration、identity、restrictions。用户已确认不需要 palette_notes，因此不再保留配色说明字段，也不再要求 colors 与配色说明二选一；colors 为可选列表。字体、主体和背景仍需有值，排版使用默认值。可选文本无内容时使用 null；调用方应将空输入转为 null，DTO 的 @NotBlank 会拒绝实际传入的空白字符串，不在服务层绕过该校验。

**Style 字段表**（内嵌在 style，不单独建 collection）：

| 字段 | Java 类型 | 必填规则 | 说明 |
|---|---|---|---|
| typography | String | 必填 | 字体及文字表现 |
| subject_style | String | 必填 | 主体表现方式 |
| background | String | 必填 | 背景要求 |
| layout | String | 使用默认值或填写 | 排版要求 |
| decoration | String | 可选 | 装饰元素 |
| identity | String | 可选 | 固定品牌识别元素 |
| restrictions | String | 可选 | 禁止事项及其他专属要求 |

`platform` 和 `placement` 按用户要求使用枚举，不建平台表或用途表。建议沿用正式项目的 `@MongoEnumValue` 约定保存稳定值：

| 枚举 | 建议值 |
|---|---|
| Platform | `XIAOHONGSHU`、`INSTAGRAM`、`TIKTOK`、`GOOGLE_BUSINESS_PROFILE`、`META_ADS` |
| Placement | `NOTE`、`PORTRAIT`、`SQUARE`、`STORY`、`COVER`、`UPDATE`、`FEED`、`LANDSCAPE` |

两个字段都合法不代表组合合法，服务端仍要校验对应关系，例如 `XIAOHONGSHU + NOTE`、`META_ADS + FEED`。平台显示名称、画布尺寸、安全区及内容限制继续由后端预设提供，不写进枚举字段。

`logos` 和 `references` 已由用户确认直接保存 file_key，不再保存 media library ID。正式图片 domain 中的所有图片引用也统一只保存 file_key；AM Console 可从现有 Media Library 选择素材，或通过 social-media 接口上传不入库图片，再把 file_key 交给新图片接口。写入时必须校验图片属于当前 merchant：素材库图片通过 merchant + file_key 解析；social-media 图片按第 6.1 节校验商家权限、服务端生成的目录归属、文件存在性与实际图片格式。当前 product-service 仅有按 media_library_id 查询的公开接口，素材库图片仍需增加真正按 file_key 查询/批量解析的能力；social-media 图片不要求存在 MediaLibrary 记录。移除关联只删除风格字段中的引用，不删除 file-service 文件或 media library 记录。

内嵌内容类型已由用户确认命名为 `Style`。即使它是独立 Java 文件，也只对应内嵌 Mongo 对象，不是 collection。所有共享 DTO/domain 类型遵循项目规范，作为顶级类。

## 3. ImageDesign：只表示一项创作

Collection：`image_designs`。下面是核心业务字段建议，表中字段均为必填。选择状态是否持久化暂不定案，不影响候选及版本结构。

| 字段 | 类型 | 必要性 |
|---|---|---|
| id | String | 图片记录 ID |
| merchant_id | String | 必填，记录归属及共享范围 |
| candidates | List<ImageCandidate> | 1–6 个候选身份，每项 id、index；不把版本和图片文件放进这个数组 |
| created_by | String | 谁创建了这项创作；不是独占所有人 |
| created_time | ZonedDateTime | 创建时间 |
| updated_time | ZonedDateTime | 最近业务变更时间，不因每次轮询而刷新 |

不用再加入 title、owner_id、task_id、place_id、locked_by、current_version_number、current_status、进度等字段。列表标题可取首轮用户要求的摘要；封面从可用输出派生。以后如果确实需要用户命名，再加入 name，不提前增加重命名接口。

如果需要维护 updated_time，并行轮次晚完成时应使用单调更新语义，不能让旧时间覆盖新时间；这不需要阻止多人发起修改。

**候选图各自拥有版本（已确认）。** 一次生成三张图，创建三个 candidate_id 和各自的 V1；列表按同一 ImageDesign 聚合，进入后分别查看图一、图二、图三的历史。每张候选的 id 稳定，candidate_id 标识持续修改的那张候选图，不能由某个输出文件代替。

### 3.1 为什么 ImageDesign 和 ImageVersion 分开存储

两者是一对多关系：ImageDesign 记录“一次创作”，ImageVersion 记录“某个逻辑版本下的一种执行结果”。拆成两个 collection 是当前建议，不是业务上只能这样存储。

```text
ImageDesign D1：一次生成三个候选
├─ 图一 C1：V1、V2、V3、V4
├─ 图二 C2：V1、V2
└─ 图三 C3：V1
```

| Collection | 保存的内容 | 上例记录数量 |
|---|---|---|
| image_designs | merchant、创建人、三个候选的稳定身份 | 1 条 |
| image_versions | 每个逻辑版本下 MODEL/AGENT 各一条记录 | 11 条（首版按单条、后续修改按 MODEL+AGENT 各一条估算） |

| 考虑点 | 分开存储的作用 |
|---|---|
| 版本持续增长 | 历史不持续堆进同一个创作文档，避免单文档越来越大 |
| 查看候选历史 | 可以按 design_id + candidate_id 单独查询及分页 |
| 更新执行结果 | 状态变化时只更新对应的 ImageVersion 记录 |
| 创作列表 | 可以按需读取版本摘要或聚合预览，不需要加载全部历史输入和输出 |

合在一个 collection 也能实现：将版本数组嵌入 ImageDesign。但用户已经要求候选独立版本、失败记录保留、历史不删除，版本数量会继续增长，因此建议“数量有界的候选身份内嵌，持续增长的版本单独存储”。代价是查询创作摘要需要读取或聚合版本信息，首次创建也需要协调创作及各候选 V1 的落库；这些成本并未因为拆分而消失。

这里的“两个表”在正式项目中实际是两个 MongoDB collection。该说明不新增 collection、字段或接口。

### 3.2 选择状态：暂不展开多人协作设计

本期不考虑多人同时选择不同版本，不引入选择同步、冲突处理或额外协作机制。工作台选择的持久化方式不影响本次确认的候选和版本结构。

保留简单的业务要求：修改请求明确携带 candidate_id 和 source_id，后端据此修改；商家内历史共享保持不变。

## 4. ImageVersion：一个逻辑版本下的一种执行结果

Collection：`image_versions`。一条 ImageVersion 记录代表某个候选图在某个逻辑版本下的一次生成路径，`execution_type` 区分 MODEL 和 AGENT。一个逻辑版本可以由一条或两条记录组成；每条记录直接保存自己的输入、输出和状态，修改来源直接保存在记录顶层。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | String | ImageVersion 记录 ID，每条 MODEL / AGENT 记录各自唯一 |
| design_id | String | 所属 ImageDesign |
| candidate_id | String | 所属候选图，必须存在于该 design.candidates 中 |
| version | Integer | 逻辑版本号，在同一候选内从 1 开始递增 |
| execution_type | ExecutionType | `MODEL` / `AGENT` |
| operation | Operation | `GENERATE` / `EDIT` |
| source_id | String，可空 | 本次修改所基于的 ImageVersion.id；首次生成或首次直接改素材图时为空 |
| input | ImageInput | 本记录实际使用的输入快照 |
| output | ImageOutput | 图片输出对象，包含 files 内嵌对象列表；每项保存 file_key、is_transparent |
| status | ExecutionStatus | QUEUED / RUNNING / SUCCEEDED / PARTIAL / FAILED |
| run_id | String | Agent 运行 ID；MODEL 记录为空 |
| started_time | ZonedDateTime | 开始执行时间，尚未开始时为空 |
| updated_time | ZonedDateTime | 最近状态更新时间 |
| error | String，可空 | 最近一次失败的可展示文本；不保存堆栈、密钥或模型原始响应 |
| created_by | String | 发起本次操作的 AM |
| created_time | ZonedDateTime | 创建时间 |

**唯一约束为 `design_id + candidate_id + version + execution_type`。** `version` 不再单独唯一，因为 V2 可以同时存在一条 MODEL 记录和一条 AGENT 记录。版本号按提交顺序分配；两条 V2 记录必须先使用同一个版本号创建，再分别执行，不能各自独立自增而得到不同版本号。

```text
ImageDesign D1
└─ 图一 C1
   ├─ ImageVersion(id=ver_101, version=1, execution_type=MODEL)
   │  ├─ input: ImageInput
   │  ├─ output
   │  └─ status
   │
   ├─ ImageVersion(id=ver_201, version=2, execution_type=MODEL)
   │  ├─ source_id = ver_101
   │  ├─ input: ImageInput
   │  ├─ output
   │  └─ status
   │
   └─ ImageVersion(id=ver_202, version=2, execution_type=AGENT)
      ├─ source_id = ver_101
      ├─ input: ImageInput
      ├─ output
      └─ status
```

逻辑版本状态由同一 `design_id + candidate_id + version` 下的记录汇总，不额外存一份 version_status。两条记录都成功是 SUCCEEDED；一条成功、一条失败是 PARTIAL；仍有记录运行中则整体为 RUNNING。首次 V1 失败可由用户手动重新生成同一候选 V1，其他重提规则见第 8.1 节。

**ImageVersion 不保存 merchant_id（已确认）。** 商家归属只保存在 ImageDesign.merchant_id。按商家查询时先查 ImageDesign，再按 design_id 查询版本；读取、修改和下载时也必须先加载 ImageDesign 并校验 merchant 权限。索引围绕 `design_id + candidate_id + version + execution_type`。

### 4.1 source_id 的含义

用户已确认 `source_id` 直接指向 `ImageVersion.id`：

```text
图一 C1

V1
  ImageVersion(id=ver_101, version=1, execution_type=MODEL)
    output = {
      files = [{file_key=f1, is_transparent=false}]
    }

V2
  ImageVersion(
    id=ver_201,
    version=2,
    execution_type=MODEL,
    source_id=ver_101
  )

  ImageVersion(
    id=ver_202,
    version=2,
    execution_type=AGENT,
    source_id=ver_101
  )
```

V2 的 MODEL 和 AGENT 都基于 `ver_101`。后端通过 `source_id` 找到来源 ImageVersion，直接取其 output.files 中唯一的 is_transparent=false 文件作为修改源。透明输出仅用于查看，不参与来源选择。

当前约束：

- source_id 必须指向同一 ImageDesign、同一 candidate 下已存在的较早 ImageVersion。
- 来源 ImageVersion 必须有且只有一个已保存、有效的 is_transparent=false 输出文件，由后端自动解析。
- 首次生成时 source_id 为空。
- 首次直接修改素材图片时 source_id 为空，按提交顺序将第一张底图记录为 input.images 中的 EDIT_SOURCE，其余辅助图记录为 EDIT_REFERENCE；文件可来自 Media Library 或 social-media 上传，均需校验商家归属。

**输出与来源规则（已确认）：**

- 透明 PNG 和不透明成品均保留，同一次执行的两种产物不拆成两个 candidate。
- 输出文件采用内嵌对象，每项包含 file_key、is_transparent，具体结构见下节。
- is_transparent=true 的输出仅供查看，不提供产品下载入口，也不能作为后续修改来源；is_transparent=false 的有效成品可下载、可继续修改。该规则针对 output.files，输入素材及 Logo 仍按各自角色处理。
- 每次执行只有一个不透明成品，Agent 的透明图与成品固定各一张。用户选择来源 ImageVersion 后，后端根据 is_transparent=false 解析唯一成品，来源参数只需 source_id，不依赖数组顺序或额外文件标识。

demo 现有 MODEL 路径直接输出单张成品，AGENT 路径返回透明图与成品这一对；正式接入沿各路径实际约定保存文件，保持每条记录的成品唯一性，不增加给 MODEL 额外生成透明层的流程。若实际输出缺少可用非透明图，或出现多个非透明图，应反馈输出结构异常，不自动取第一张充当来源。

### 4.2 内嵌对象字段定义

**ImageInput**：只保存三个业务输入字段。

| 字段 | 类型 | 必填/默认 | 说明 |
|---|---|---|---|
| prompt | String | 必填 | 本记录最终实际使用的提示词；改图包含用户修改要求与后端实际追加的保护规则，不含临时图片 URL |
| images | List<ImageReference> | 默认空 | 本记录实际使用的有序图片引用，包含本轮实际发送的 EDIT_REFERENCE |
| brand_style_id | String | 可空 | 所选品牌风格；未使用 PRESET 风格时为空 |

**ImageOutput**（在 ImageVersion.output 中）

| 字段 | 类型 | 必填/默认 | 说明 |
|---|---|---|---|
| files | List<ImageOutputFile> | 默认空 | 输出文件对象列表；Agent 完整产物为一张透明图和一张不透明图，成品唯一；透明输出仅查看 |

**ImageOutputFile**（内嵌在 ImageOutput.files 中）

| 字段 | Java 类型 | 必填/默认 | 说明 |
|---|---|---|---|
| file_key | String | 必填 | file-service 的稳定图片文件引用 |
| is_transparent | Boolean | 必填，由后端检测 | 图片是否包含真实透明或半透明像素；为 true 的输出仅供查看 |

`ImageOutputFile` 是顶级 Java 值对象，持久化为 `files` 数组中的内嵌对象，不新增 collection。生成结果与相关接口响应统一使用该结构，不再返回只有文件字符串的输出列表。

```json
{
  "output": {
    "files": [
      {"file_key": "layer.png", "is_transparent": true},
      {"file_key": "result.png", "is_transparent": false}
    ]
  }
}
```

`is_transparent` 由后端解码每张图片后分别计算，不依赖文件名、返回顺序或仅检查是否存在 alpha 通道。任一像素 alpha 小于 255 即为 true（全透明图片也为 true），所有像素均不透明则为 false；无法解码时按图片处理失败反馈，不能默认为 false。Java 可使用标准库 ImageIO 完成检测。该字段仍表达透明度事实，不表示图片的 FINAL/LAYER 角色；本期产品规则明确 true 仅供查看，false 的有效输出可下载、可继续修改。含透明边缘的完整成品也会标记 true，并按该规则处理。

**ImageReference**（在 ImageInput.images 中）

| 字段 | 类型 | 必填/默认 | 说明 |
|---|---|---|---|
| role | ImageRole | 必填 | CONTENT / LOGO / STYLE_REFERENCE / EDIT_SOURCE / EDIT_REFERENCE |
| file_key | String | 必填 | 稳定文件引用 |

`EDIT_REFERENCE` 表示本轮改图的辅助参考图片：首次直接改素材时为第一张底图之后的图片，后续修改时为 reference_file_keys 新附加的图片。它与作为编辑底图的 `EDIT_SOURCE`、首次生成的内容素材 `CONTENT` 和风格图 `STYLE_REFERENCE` 区分。角色由后端根据来源与用途填入。每条 MODEL / AGENT 记录只保存自己实际使用的图片；本轮参考图已产生的视觉修改可以随来源成品保留，但这些参考文件不会自动重新附带到下一轮。

**首次直接改素材的图片分工（已确认）：** 沿用 Demo 此入口一次提交 1–5 张图片的限制，第一张固定为待修改的底图，后续 0–4 张为辅助参考；不额外增加主图 ID 或让前端自由指定 role。后端在实际 prompt 中明确“修改第一张图，其余图仅按本轮要求提供参考”，并以第一张底图确定目标画布。即使辅助图片实际是 Logo，也按本轮 EDIT_REFERENCE 保存，不自动把它变成后续轮次持续携带的 LOGO。用户可明确要求更换 Logo、调整文字等，图片顺序与角色说明一起保留在本次输入中。

**ImageCandidate** 仍在 ImageDesign.candidates 中：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| id | String | 必填 | 稳定候选 ID，各版本通过 candidate_id 引用 |
| index | Integer | 必填 | 展示顺序，建议从 1 开始；不是版本号 |

### 4.3 输入快照和分支取舍

每条 ImageVersion 通过 input 保存本次实际使用的 prompt、图片引用和品牌风格 ID。平台、画布参数、临时 endpoint 及调用参数继续存在于请求 DTO 或调用日志中，不进入核心 domain；当前不保存输出图片的 width/height。

**图片模型策略已确认：有且只有一个，先 hardcode 在后端代码中。** 首次生成和后续 MODEL 改图使用同一个代码常量，不引入模型配置项、默认模型切换逻辑、模型选项列表或前端模型参数；domain 和输入快照不保存模型字段。具体模型标识在实现时集中定义，调用日志可记录实际固定模型。MODEL / AGENT 仍表示两种执行路径，单一图片模型不改变既有双链路设计。

**通用规范维护方式已确认：本期先由后端统一维护。** 文案准确、素材使用、Logo 和可读性等通用要求集中维护，提示词优化、首次整图生成和风格预览复用同一来源；不迁入 demo 的 AM 页面编辑与独立 brand_rules 管理接口，也不新增对应 collection。具体采用项目已有配置或代码中的统一定义，在技术设计时确定，不引入独立配置管理功能。通用规范在请求组装时进入实际使用的 prompt，历史与已提交执行保留当时的实际输入；MODEL / AGENT 改图按各自实际采用的保护规则保存输入快照。

**Agent 后续改图输入已确认：保留后端保护规则。** 采用最新整图 Demo 的约束：以选中的当前成品为编辑底图，保持其画幅；只做本轮要求的修改，尽量保留其他内容、文字、构图和风格。菜品按完整照片或餐盘保留，仅改文字时避免重绘食物；用户明确要求修改某项内容时，允许该项修改。附加图片仅作为本轮参考，不据此整体重建或替换编辑底图。这些是模型输入约束，不表示能保证像素完全不变。

后端将上述本轮实际采用的规则、画布要求、图片角色说明和用户修改要求一起固化到 input.prompt；input.images 保存同序的 role + file_key。调用 Agent 时再将稳定引用解析为 URL 并拼入发送文本，临时 URL 和 base64 不写入 domain。首次失败 V1 的重新生成仍复用已保存输入，不因后端规则更新而改用新规则。本项沿用 ImageInput 的三个字段，不新增原始提示词、规则副本字段或独立 collection。

**无品牌风格模式已确认：不自动补商家资料。** 选择 NONE / 无品牌风格时，提示词优化及首次生图以 AM 的提示词和所选图片为内容来源，不自动从 RestaurantBrandProfile 补入餐厅名称、菜系、定位或门店资料。merchant_id 保持归属和权限作用；后端通用规范及用户已选平台规则继续应用。视觉风格名称只表示风格标签，不应作为真实餐厅名称自动写入画面。

后续如果只保留一种生成方式：

- 只保留 AGENT：停止创建 execution_type=MODEL，只创建 AGENT；历史 MODEL 记录保留读取。
- 只保留 MODEL：停止创建 execution_type=AGENT，只创建 MODEL；历史 AGENT 记录保留读取。
- 不在本期立即删除历史类型的数据，避免已被后续版本引用的 file_key 和来源链丢失。
- 不增加 media_type、视频字段或图片/视频父 domain；视频另行设计。

**prompt_mode 仍保留在生成请求中。** demo 的 run_generation 根据 prompt_optimized 切换系统提示词、是否附风格图以及是否再拼品牌描述；正式请求仍支持该模式，ImageInput 只保存最终实际 prompt，不持久化 prompt_mode。首次 V1 手动重新生成复用已保存的最终 prompt 和有序图片引用，不重新执行提示词优化；尺寸等非 domain 调用参数需要从可恢复的执行上下文取得，不能依赖临时内存或重读最新风格改变原始请求。

**GENERATE 不一定调用 /images/generations。** demo 有参考图时实际调用 /images/edits，没有参考图才调用 /images/generations；业务 operation 仍是 GENERATE。endpoint 由后端选择。
## 5. 状态与恢复的最低要求

超时即失败、首次失败 V1 的人工重新生成等已确认规则按下文执行；其余恢复机制为技术建议，不改变用户已确认的“同一图片允许多轮同时运行”。

- 状态统一为 QUEUED、RUNNING、SUCCEEDED、PARTIAL、FAILED；不引入 UNKNOWN。超时按本系统未在时限内完成处理，无需等待远端确认停止再标记失败。
- 输出完整成功需预期产物均已保存到 file-service，并在 output.files 中有对应 file_key、is_transparent；其中必须有且只有一张有效的非透明成品。成对返回的透明图与成品均需保存。接口契约建议将不完整交付进一步区分为：成品已保存但透明图缺失为 PARTIAL；缺少有效非透明成品为 FAILED，已保存的透明图仍可查看。保存失败不重做已完成图片，具体见第 11.8 节。
- **画幅验收（已确认）：** 返回成品的比例明显错误时，将该条 ImageVersion 判为 FAILED，error 记录画幅不符原因；错误成品不作为可下载或可继续修改的成功输出。MODEL / AGENT 及首次生成、直接改素材、后续修改统一遵循该规则。不通过裁切、拉伸或补边将明显错误的比例修正为成功结果；比例一致、仅像素尺寸不同可等比缩放到目标尺寸。微小取整误差的容差在技术设计时统一，不将 demo 当前 2% 阈值直接视为已确认的产品规则。
- 画幅错误只影响对应执行记录；该失败记录和已分配版本号保留，不覆盖历史成品、不自动重发。另一条执行成功时，逻辑版本可汇总为 PARTIAL；两条都失败时为 FAILED，仍有执行运行时保持 RUNNING。
- 同一逻辑版本的一条 MODEL/AGENT 记录失败时，另一条已保存的图片仍可查看；其中非透明的有效图片可下载和继续修改，透明输出仅供查看。逻辑版本可汇总为 PARTIAL。
- 仍有执行运行时整体显示 RUNNING，同时返回已产生输出；整轮终态取决于所有执行。记录列表另返回 has_usable_output，按有效且非透明的输出计算，避免最近失败遮掉历史可用成品。透明输出仍可查看，但不计为可下载或可继续修改的成品。
- **超时即失败（已确认）：** 同步图片模型调用超时、Agent 执行等待超时，均将对应 ImageVersion 标记为 FAILED，error 显示超时原因；触发响应丢失而无法取得执行结果时也按失败反馈。有无 run_id 不改变失败展示与首次 V1 手动重新生成规则，不增加“结果未确认”的业务状态或单独人工确认流程。具体超时阈值在技术设计中统一。
- 超时只影响对应执行记录，另一条链路和其他候选继续正常执行；已有有效结果按既有规则使用。到达失败终态后，迟到响应、旧轮询或旧回调不得将该记录自动改为成功，也不得覆盖用户重新生成的当前结果；不自动重新触发生成。
- 远端成功但文件入库失败：Agent 可利用原 run_id 重新取得 artifacts；已完成的输出图片不重做。同步网关若只返回图片字节且这些字节尚未保存就进程退出，现有代码不能保证找回。
- 服务重启时扫描本功能未完成执行；已超过执行期限的记录按超时失败收敛，未超时且已有 run_id 的记录可继续查询同一 run，不重新触发生成。不照搬 demo 全量标记失败，更不能影响别的营销 Task。
- **失败 V1 的人工重新生成（本轮补充确认）：** 只有用户主动点击才重新执行该候选的 V1，包含因超时失败的 V1；复用同一条 ImageVersion，id、design_id、candidate_id 和 version=1 均不变，其余候选不受影响。只保留最近一次状态、错误和输出，不保存历次尝试。重新生成复用原 input，更新 status、error、output、run_id、started_time 和 updated_time；新执行不沿用上一次的输出、错误或 run_id，成功后 error 为空，created_by 和 created_time 保持原创建信息。执行中拒绝重复启动，已成功的 V1 不因此开放任意覆盖。复用原输入，不增加 source_id，不自动重新付费调用；按钮请求需幂等，并隔离不同次执行的旧回调。

少量技术字段可能仍必要：每条 ImageVersion 记录的 started_time/updated_time，以及可恢复的远端 artifact 引用。它们用于标识执行阶段和避免旧响应覆盖新结果；不是新的业务表。失败 V1 的手动重新生成只保留最近一次执行结果（已确认），不增加 attempt 字段或执行历史 collection；重复请求防护及旧回调隔离仍需在技术实现中处理。

创建/改图可通过 request_id 实现请求幂等；版本粒度作用域需要包含操作类型、design_id、candidate_id、本次操作人 ID（内部 operator_id）及 request_id，防止不同候选的 V1 因同一次批量请求而互相冲突；本次操作人不一定是记录原 created_by。首次创建要关联一次请求对应的 design、候选及各 V1；具体落库原子性留到技术设计。幂等防止同一次请求重复执行，不限制不同用户或不同请求并行，不能仅靠前端按钮禁用。

透明检测已补做内存小样本验证：demo 的初版逻辑对调色板透明、全透明等情况存在漏判，且不能直接按返回顺序判断产物角色。正式接入应逐张检测，校验唯一非透明成品，并保持第 4.1 节的查看、下载及修改来源规则。尚未调用 Agent 验证真实产物形态。

## 6. 接口收敛建议

**用户已确认：现阶段不提供图片历史删除功能。** 整条创作、单张候选及单个版本均不增加删除接口，保留候选和版本；不为此增加 deleted、deleted_time 等软删除字段。该决定仅针对图片历史，品牌风格管理的删除接口不在此次讨论范围内。

品牌风格接口包括创建、编辑、列表与详情查询、删除、预览；已有素材选择复用 Media Library 接口，新上传图片可使用 social-media 接口而不进入素材库。后续修改不暴露 candidate_count，保持一组 MODEL / AGENT 双链路结果。新增接口不接入 Task。

通用规范由后端统一维护，不提供单独的页面编辑及管理接口。品牌风格预览仍可包含组装后的通用规范，不增加规则管理 collection。

| AJAX 接口（建议） | 核心输入 | 返回 |
|---|---|---|
| POST /ajax/media-library/social-media/upload | merchant_id、filename、file_base64 | 201：file_key、url；仅保存文件，不创建素材库记录 |
| PUT /ajax/restaurant-brand-style/search | merchant_id、可选 platform/placement、skip、limit | total、风格摘要列表 |
| GET /ajax/restaurant-brand-style/:id | 风格 ID | 风格详情，图片 URL 仅在 View 中派生 |
| POST /ajax/restaurant-brand-style | merchant_id、name、summary、platform、placement、colors、logos、references、style | 201：新风格详情 |
| PUT /ajax/restaurant-brand-style/:id | 全量风格内容，不允许修改 merchant_id | 更新后的风格详情 |
| DELETE /ajax/restaurant-brand-style/:id | 风格 ID | 204；删除风格记录，不删除引用文件或图片历史 |
| POST /ajax/restaurant-brand-style/preview | merchant_id 和未保存的风格表单 | prompt；仅预览规范文本，不生成图片、不保存风格 |
| POST /ajax/image-design | merchant_id、operation、prompt_mode/prompt、素材及风格/平台选择、candidate_count | 202：design_id、candidates，每项有 candidate_id 及 V1 的 ImageVersion 记录列表 |
| PUT /ajax/image-design/search | merchant_id、可选状态/类型、skip、limit | total、精简记录列表 |
| GET /ajax/image-design/:id | ID | 创作基本信息、各候选的最新版本及运行摘要、各自可用预览 |
| GET /ajax/image-design/:id/candidate/:candidate_id/version | skip、limit | total、按 version 分组的版本摘要 |
| GET /ajax/image-design/:id/candidate/:candidate_id/version/:version | candidate_id、version | 该逻辑版本下的全部 ImageVersion 记录，按 execution_type 区分 MODEL/AGENT |
| POST /ajax/image-design/:id/candidate/:candidate_id/edit | instruction、source_id、request_id、可选 reference_file_keys（最多 5 张） | 202：本候选的新 version 及 MODEL/AGENT ImageVersion 初始记录 |
| POST /ajax/image-design/:id/candidate/:candidate_id/version/1/regenerate | request_id；服务端复用原 V1 输入 | 202：原 ImageVersion.id、design_id、candidate_id、version=1 的最新执行状态；仅失败的首次 V1 可手动重新生成 |
| GET /ajax/image-design/:id/candidate/:candidate_id/version/:version/record/:record_id/download | record_id | 经归属校验后下载该记录唯一的非透明成品；透明图不提供下载入口 |
| GET /ajax/image-option | 无 | 平台/用途预设、画幅与数量等限制；不返回模型选项 |
| POST /ajax/image-prompt/optimize | merchant_id、当前 prompt/素材/风格/平台上下文 | prompt |

版本查询和修改明确限定 candidate_id；一次创建返回多个候选各自的 V1 记录。V2 等逻辑版本按 version 分组返回。candidate_id 独立版本已确认；首次失败 V1 的人工重新生成使用单独入口，其他历史版本不增加原位重试。多人选择同步仍不列入当前核心接口。

详情接口不一次装载无限版本；逻辑版本详情包含该 version 下一条或两条 MODEL/AGENT 记录，输出按 output.files 返回 file_key、is_transparent 内嵌对象。下载按 record_id 获取唯一非透明成品，无需文件下标或额外文件选择参数。图片字节不进入 Mongo 或前端 JSON。列表状态筛选必须在数据库聚合/查询中先完成再分页，不能先取一页再内存筛状态。初期可以使用现有项目已有的 Mongo 聚合能力，不为状态查询增建统计 collection；如果后续测量证实查询慢，再引入最少汇总字段。

source_id 是请求中的实际编辑来源，必须指向同一 Design、同一 candidate 下的既有 ImageVersion；后端执行来源校验，并从 output.files 解析唯一的、已保存且有效的 is_transparent=false 图片作为修改源，不接受额外来源文件选择参数。首次直接改素材时 source_id 为空，创建请求按顺序提交图片 file_key：第一张作为 EDIT_SOURCE，其余作为 EDIT_REFERENCE；服务端校验文件来源及商家归属。轮询回来一个新版本不能自动更换用户本次提交的来源。

**本轮附加参考图（已确认保留）：** 后续修改请求增加 reference_file_keys，有序字符串列表，默认空、最多 5 张，归一化时去重并保持首次出现的顺序。参考图片可来自 Media Library 或 social-media 上传，使用前按商家范围及文件来源校验；它们作为辅助参考，不代替 source_id 所指的编辑底图。

| 修改请求字段 | 类型 | 规则 |
|---|---|---|
| instruction | String | 本轮修改要求 |
| source_id | String | 来源 ImageVersion.id，由后端取得其唯一非透明成品 |
| reference_file_keys | List<String> | 可选，默认空，最多 5 张，仅本轮使用 |
| request_id | String | 用于本次提交的幂等标识，具体技术约束保持第 5 节建议 |

两条链路都使用本轮参考图并记录实际输入顺序：MODEL 为原始 CONTENT、选定 LOGO、当前 EDIT_SOURCE、按请求顺序排列的 EDIT_REFERENCE；AGENT 为当前 EDIT_SOURCE、按请求顺序排列的 EDIT_REFERENCE。相应角色说明进入本轮实际 prompt，图片在外部调用时按同样顺序解析为 URL 或模型支持的传输格式。每轮参考图只记入现有 input.images，不增加 ImageInput 顶层字段或独立 collection。下一轮未提交 reference_file_keys 时按空列表处理。

### 两层服务契约

AM Console 使用 CurrentSession 取得操作人，向 integration 传内部 operator/user_id。外部 AJAX 请求不接受任意 user_id、agent_id、endpoint 或 created_by。查询和写入均验证商家范围，而不只在生成按钮入口验证。

新增图片接口不接受 model、image_model 等模型选择参数；图片模型由后端唯一固定值决定，AM Console 和 integration 均不增加模型选择 DTO 字段。

沿用现有项目的 `@Property` snake_case、Request/Response/View 与 domain 分离、skip/limit 分页。JSON DTO 的 String 使用 @NotBlank，必填字段再加 @NotNull；列表限制与跨字段业务校验区分，不重复手写已由注解表达的空值校验。

核心错误只需区分：商家不可访问、风格/素材不存在或不属于商家、平台用途冲突、来源不是可用完整图、输出画幅不符、执行超时或调用失败。画幅不符通过对应异步执行记录的 FAILED 和可读 error 反馈，错误标识可沿用 output_aspect_mismatch；执行超时同样使用 FAILED，error 说明超时，不返回“结果未确认”状态。错误返回稳定代码及可读消息，不把模型原始响应、密钥或堆栈直接返回页面。

### 6.1 social-media 图片上传（不入素材库）

**已确认：** 新增 `POST /ajax/media-library/social-media/upload`，用于上传本功能所需但不进入素材库的图片。文件可以继续被创作、版本及品牌风格引用，不增加自动过期、定时清理或提交后迁移到其他目录的流程，也不新增上传记录 collection。

container 仍为 `static`，用户指定的是 container 内的 blob 路径。后端沿用 file-service 的 azure.basePath 环境配置，构造 dirs 为 `["media-library", merchant_id, "social-media"]`，UUID 文件名由后端生成，不由前端指定目录。

| 环境 | container 内路径 |
|---|---|
| 开发 | fbr-dev/product/media-library/{merchant_id}/social-media/{uuid}.png |
| UAT | fbr-uat/product/media-library/{merchant_id}/social-media/{uuid}.png |
| 生产 | fbr-prod/product/media-library/{merchant_id}/social-media/{uuid}.png |

请求与响应建议保持最小结构：

```json
{
  "merchant_id": "merchant_001",
  "filename": "reference.png",
  "file_base64": "<图片的base64内容>"
}
```

成功保存后返回 201：

```json
{
  "file_key": "/fbr-dev/product/media-library/merchant_001/social-media/<uuid>.png",
  "url": "<由CDN生成的展示URL>"
}
```

该接口只上传图片，不需要 category、tags、task_id 或 media_library_id。file_key 作为稳定引用写入 ImageInput.images 或风格的 logos/references；url 用于展示，不写入核心 domain。PNG 文件名须对应真实 PNG 内容；支持的输入格式及转换方式在接口技术定稿时明确，不能仅修改扩展名。保存时的透明度和尺寸处理沿用第 7.2 节的文件保存约束。

上传时先校验当前用户可访问 merchant，再由服务端生成所属商家的路径并保存。后续使用 social-media file_key 时，仍需校验用户的 merchant 权限、规范化后的环境与商家目录边界、文件存在性及实际图片内容，不能只信任客户端传入的字符串，也不能因缺少 MediaLibrary 记录就拒绝合法的 social-media 文件。

本期不配置文件 TTL，也不为已使用文件增加自动复制流程。后续若单独引入存储生命周期规则，应保护历史已引用文件；当前未核实线上存储是否已有其他清理策略，不能将本期“不新增自动清理”理解为已验证云端永久保留。

## 7. 已核对的复用事实与限制

### 7.1 “能选择 merchant”已有具体代码依据

AM Console 的 MerchantService.search 会把当前 sessionUserId 写入内部请求的 primaryAMUserId。Integration 的 MerchantService.search 对管理员不加 AM 过滤，对非管理员限定 merchant.primary_am_user_id。

因此当前规则可以理解为“管理员或该 merchant 当前主要 AM 可见”。图片应复用相同判定，不再按图片创建人限制，也不借本任务新增第二套 AM 成员表。这意味着 AM-A 创建后，交接给 AM-B，或者管理员访问时，都应看到历史。它不意味着当前系统已经支持任意两个普通 AM 同时被指定为主要 AM。

现有 merchant 详情路径没有在已读方法中体现同等范围校验。列表筛选不是完整授权接口，因此新图片接口需要在服务端统一执行同样规则；不是通过搜索第一页再检查 merchant 是否在其中。

### 7.2 文件服务可复用，但不等同下载接口已完成

FileWebService.upload/copy 可提供 file_key；downloadFile 返回 file_base64。图片 BFF 应用已有 CDNService 生成展示 URL，下载可以输出文件响应或采用项目允许的下载 URL。仅当确认现有 CDN 暴露策略满足需求时才直接返回 CDN 链接；本轮未验证链接访问控制和文件保留策略。

MediaLibrary 的 size 是文件体积，不是像素尺寸。当前 ImageOutput 不保存 width/height；后续如需展示尺寸，可从图片元数据读取。改图画布继承来源；按已确认规则，成品比例明显错误时先判对应执行失败，比例一致而像素尺寸不同才等比缩放。具体容差与图片编码属于技术细化，不直接照搬 demo 各分支当前不同的补边或拒绝逻辑。

进一步核对发现，FileService.upload 的图片路径会调用 transformImage；MagickClient.compressAndCorp 默认使用 `1920x10000>` 限制尺寸，配置 compress_image_size 时还可能把 PNG 转为 JPG 并去掉 alpha。因此保留透明 PNG 和来源精确尺寸不能仅靠传入 PNG 文件名保证。接入时需选择保持图片字节、透明度及尺寸的保存通路；现有 copy 和直传 URL 能力可继续核实，不因此新增业务 domain。is_transparent 必须对应最终保存文件的真实透明属性，不能只检测处理前图片。

素材库选择、social-media 上传及历史引用的校验需分开：素材库图片按 merchant + file_key 解析归属；social-media 图片按第 6.1 节校验商家目录、权限和文件，无需素材库记录；历史输入和生成输出根据所属 ImageDesign 的权限、已保存引用及文件存在性访问，不要求它们每次仍在 Media Library 中。现有 product-service 的删除方法删除素材记录并发送消息，该方法本身不直接删除底层文件；本轮未追踪所有消息消费者及存储保留策略，不将它推断为文件永久保留。RestaurantBrandProfileService.imageUrlByKey 虽名为按 key 解析，实际使用 request.ids 查询并按返回 id 建图，不能直接当作按 file_key 解析的现成能力。

### 7.3 客户端可以复用，执行可靠性不能假定已有

Core AI Agent 客户端可复用 trigger/get 和文件查询；图片网关客户端在已搜索的模块未找到。现有 CoreAIModule 设置了 maxRetries(3)，当前未核实底层重试条件。业务层仅对首次失败 V1 提供用户主动点击的重新生成入口，不做自动重试；接入付费生成前仍需确认底层是否会自动重发，并通过 request_id 防止同一按钮请求重复执行。

## 8. 业务场景、查询语义与技术细化项

以下语义不增加新的业务 collection；后续修改失败占版本号的决定保持。首次 V1 失败可手动重新生成原候选的 V1，是本轮对先前“不提供重试”的明确补充。其余未标注确认的内容仍为建议。

### 8.1 首次创建与失败处理

**后续修改失败也保留版本号（已确认）。** 例如 V1 后的修改即使图片接口和 Agent 都失败，仍保留失败的 V2，供查看失败原因；重新提交修改要求会创建 V3。当前新增的原位重新生成规则仅覆盖首次失败 V1，不将它自动扩展为任意历史版本覆盖。

**首次失败 V1 可人工重新生成（本轮补充确认）。** 例如图二 C2/V1 初次失败后显示失败；用户可点击该候选的“重新生成”按钮，仍生成 C2/V1，保留同一 ImageVersion.id、design_id、candidate_id 和 version=1，不创建新创作，不升级为 V2，不重做图一与图三。不点击按钮就一直保持失败。只保留最近一次状态、错误和输出，不保存每次尝试的历史。例如前三次失败、第四次成功后，C2/V1 只展示第四次的成功结果；不增加 attempt 字段或执行历史表。

demo 的 `poster_edit.create_batch` 先创建 N 个候选任务，`_run_tasks` 再分别调用 `_run_candidate`；每个候选独立触发 Agent 并保存 run_id。因此正式版将它们对应到 N 个候选的各自 V1，不需要把一个共享 Agent run 拆给多个候选，也不能把透明层和完整图拆成两个候选。

| 场景 | 建议结果 |
|---|---|
| 首次请求三张图 | 一个 Design；C1/V1、C2/V1、C3/V1；候选身份在模型完成前确定 |
| C2 首次生成失败 | C2/V1 显示失败并提供“重新生成”；不点击则保持失败，C1、C3 已有图正常可用，不缩减或重排候选编号 |
| C2/V1 超时，远端是否完成无法确认 | 直接标记 FAILED 并显示超时，可手动重新生成同一 V1；不等待人工确认，不自动重试 |
| 用户点击 C2/V1 的“重新生成” | 复用同一 ImageVersion.id 和原输入再次执行 C2/V1，只保留最近一次状态、错误和输出；不新建 Design、candidate、V2 或尝试历史 |
| C2/V1 超时失败后收到旧执行结果 | 旧响应不得将失败自动改为成功，也不得覆盖已经重新生成的状态、错误或输出 |
| 修改 C1/V1 的指定完整图 | 创建 C1/V2；分别新增 execution_type=MODEL 和 AGENT 两条 ImageVersion 记录 |
| C1/V2 的 MODEL 成功、AGENT 失败 | V2 整体为部分成功；MODEL 的非透明有效输出可下载、可继续修改，透明输出仅查看；AGENT 记录保留失败原因，当前不重试 |
| C1/V2 的 AGENT 返回明显错误比例，MODEL 成功 | AGENT 记录标记 FAILED 并记录画幅不符；MODEL 成品正常可用，V2 为 PARTIAL，旧版本继续保留，不自动重试 |
| C1 已有 V3，重新从 V1 的 MODEL 记录修改 | 创建 C1/V4，两条记录 source_id 均指向 V1 的 MODEL ImageVersion.id |
| V3 先于 V2 完成 | 版本号不变；V2 晚返回不能覆盖 V3 或把 V3 改名 |

202 表示请求与待执行记录已持久化并被受理，不表示已经出图。首次创建含多份版本记录，必须保证重发同一请求能拿回同一组身份、初始化缺失记录可恢复，再派发模型调用；不能把“已返回三个 ID”当作完整落库保证。具体事务或恢复机制留在技术设计时决定。

### 8.2 版本排序、编辑来源、预览分别表达

版本号按提交时分配，在单个候选内递增；版本完成时间不能决定编号。正式项目已有 `TaskSequenceService` 通过 Mongo `findAndModify + $inc` 原子分配编号的先例，可以借鉴机制，但不复用 AM Task 的全局序列，也不直接为图片新增序列表。候选内原子计数或唯一索引冲突的处理方式留到实现设计；不承诺数据库异常下编号绝对无空缺。

详情返回每个候选的三个视图信息即可：

- `latest_version`：版本号最大的已创建版本，可能还在运行或失败。
- `active_versions`：尚未结束的版本摘要，可同时有多个，不假设只有一个 running_version。
- `preview_output`：按明确默认规则选出的可用输出图片，并带 candidate_id/version/execution_type/record_id；记录内使用唯一非透明成品，最新版本没有完整图时可以回看旧版本。它只用于展示，不代表用户已经选择下一轮来源。

这些是查询结果，不是要求 Mongo 再增加三组冗余字段。第 11.8 节将默认预览细化为：最高版本号中有效的不透明成品，同版本优先 MODEL、其次 AGENT；不以模型返回先后顺序决定默认封面。该值仅供初始展示，用户实际选择的来源仍由 source_id 明确表达。

提交修改必须使用用户实际选择的 source_id。轮询发现新版或另一位 AM 的修改完成，都不能悄悄更换用户本次编辑来源。

### 8.3 核心修改接口的最小例子

接口建议：`POST /ajax/image-design/D1/candidate/C1/edit`。

```json
{
  "source_id": "ver_101",
  "instruction": "把标题放大，其他内容保持不变",
  "reference_file_keys": [
    "/fbr-dev/product/media-library/merchant_001/social-media/<uuid>.png"
  ],
  "request_id": "本次提交的唯一标识"
}
```

reference_file_keys 可省略或传空列表；只有本轮需要附加参考图片时才传，最多 5 张。它指定辅助参考文件，不改变 source_id 的唯一成品来源语义。

`request_id` 是防止同一次网络请求重发造成重复生成的建议技术字段；不会限制 AM 主动提交新的修改。来源验证检查 merchant 权限、design、candidate，再从 source_id 对应记录解析唯一有效的非透明文件；无需前端指定文件 key 或下标。成功受理返回 candidate_id、新 version，以及该版本下 MODEL/AGENT ImageVersion 记录；前端按 candidate_id + version 查询本轮结果。

无需传全局当前版本、整份旧图片数据、merchant_id 或两条链路各自的改图请求。服务端从 design 确定商家，以 source_id 对应 ImageVersion 中唯一的非透明输出作为修改源，从历史快照构建 MODEL/AGENT 两条记录。

### 8.4 多人选择场景的范围

本期不考虑 AM-A 与 AM-B 同时选择不同版本的情况，不扩展选择同步和冲突处理。商家内历史共享、每张候选独立版本，以及请求显式指定 source_id 的规则保持不变。

### 8.5 接口定稿时需补齐的技术契约

本次讨论的输出结构、透明图用途、单一固定模型、通用规范维护方式、每轮附加参考图及画幅验收已并入正文，已确认的决定不重新开启。下表的请求字段、响应投影和路由已在第 11 节补为接口契约建议；实际 Java DTO 和工程验证仍未实施。不将本次文档整理视为已批准实施。

| 衔接点 | 建议默认行为与边界 |
|---|---|
| 三种入口 | 首次生成走 MODEL；首次直接改素材走 AGENT；已有候选继续修改走 MODEL + AGENT。后续双链路沿用已有决定，两个首次入口补成明确契约，不开放任意 execution_type 组合 |
| 首次直接改素材的分工 | 已确认第一张为修改底图 EDIT_SOURCE，其余为本轮 EDIT_REFERENCE；沿用此入口总计 1–5 张，后端明确告诉 Agent 各图片用途，source_id 为空，不增加主图 ID 或新 role |
| 画布 | 首次直接改素材以第一张修改底图为目标画布，保留图片顺序；后续修改继承来源文件的精确尺寸，不扩 domain 尺寸字段。返回比例明显错误时判该条执行失败已确认；比例一致、仅分辨率不同可等比缩放，具体数值容差留作技术细化 |
| 从 AGENT 结果继续修改 | MODEL 分支需要的原始 CONTENT/LOGO 沿该候选的来源链取得；不能只读取所选 AGENT 记录的 input.images，也不把未实际发送的素材伪装成 AGENT 输入。首次直接改素材未选择 CONTENT/LOGO 时不补造这些角色；后续以选定成品为当前 EDIT_SOURCE，携带本轮新提交的 EDIT_REFERENCE，不自动重新附带首次的辅助参考图片 |
| 本轮附加参考图 | 已确认每轮最多 5 张，两条链路均接收；使用 EDIT_REFERENCE 记入各自 input.images，不自动在下一轮再次附带，保持请求顺序并校验商家归属 |
| Agent 后续改图输入 | 已确认保留后端追加的画布、修改范围及素材保护规则；与用户修改要求一起保存到 input.prompt，角色与稳定引用按顺序保存到 input.images，发送时才拼图片 URL。约束允许用户明确要求的对应修改，不另增输入字段 |
| 风格变更与历史 | 提交时固化最终 prompt 和本次实际图片引用；排队执行及历史读取不依赖风格仍存在或重新读取最新风格。后续修改基于选中来源及原始素材，不因为风格更新而悄悄换规范；保持现有三个 ImageInput 字段，不保存整份品牌资料副本 |
| 列表类型与状态 | 类型建议按首次创建入口 GENERATE / EDIT 区分；创作列表状态与单条执行状态分层，按“存在运行中执行→进行中；否则存在非透明有效输出→有可用成品；否则→暂无可用成品”聚合，同时返回候选最新状态和可用输出标记。仅有透明输出时仍可查看，不能据此宣称有可编辑成品。具体状态值在接口 DTO 中统一，不新增统计 collection |
| 输出展示 | domain 仍只有 file_key、is_transparent；展示 URL 在 View/BFF 中按已有 CDN 约定派生，不写回 domain。透明输出仅展示，下载接口拒绝透明输出；显示与下载均需保留归属校验，不能将 file-service 的 base64 响应误当下载链接 |
| social-media 上传 | 已确认仅表示不入素材库；按环境配置及商家目录保存文件，不新增 TTL、清理、转存流程或 collection。接口需支持 social-media 文件引用校验，不能沿用“必须在 MediaLibrary 中存在”的单一路径 |

素材按 file_key 的真正解析能力、透明文件的保存通路、异步请求参数如何可靠交接、版本分配及请求幂等属于工程核实，不作为本轮新增业务问题。上述默认建议应在后续接口定稿时写成明确契约。

## 9. 代码依据

- [AM Console 将会话用户带入商家搜索](D:/java/DRI/fbr-project/frontend/am-console-site/src/main/java/app/amconsolesite/service/MerchantService.java:33)
- [Integration 的管理员/主要 AM 筛选](D:/java/DRI/fbr-project/integration/ai-marketing-integration-service/src/main/java/app/aimarketingintegration/service/MerchantService.java:93)
- [管理员角色判定](D:/java/DRI/fbr-project/integration/ai-marketing-integration-service/src/main/java/app/aimarketingintegration/service/UserRoleService.java)
- [已有 Mongo 查询及聚合风格](D:/java/DRI/fbr-project/integration/ai-marketing-integration-service/src/main/java/app/aimarketingintegration/service/TaskQueryService.java)
- [现有原子编号分配机制](D:/java/DRI/fbr-project/integration/ai-marketing-integration-service/src/main/java/app/aimarketingintegration/service/TaskSequenceService.java)
- [demo 多候选独立 Agent 调用](D:/ai_project/social-media/backend/app/services/poster_edit.py:79)
- [文件下载返回 base64](D:/java/DRI/fbr-project/backend/file-service-interface/src/main/java/app/file/api/file/DownloadFileResponse.java:14)
- [Core AI 客户端配置](D:/java/DRI/fbr-project/library/core-ai-library/src/main/java/app/coreai/CoreAIModule.java:20)
- [demo 优化模式与端点选择](D:/ai_project/social-media/backend/app/services/whole_image.py:498)
- [demo Agent 查询与超时](D:/ai_project/social-media/backend/app/services/core_ai.py:250)
- [demo 图片角色识别与透明图回退](D:/ai_project/social-media/backend/app/services/agent_image.py:89)
- [demo 首次生成保存模型、品牌及通用规范](D:/ai_project/social-media/backend/app/routes/whole_image.py:570)
- [demo 后续改图沿用初始模型](D:/ai_project/social-media/backend/app/services/whole_image.py:948)
- [demo 通用规范维护接口](D:/ai_project/social-media/backend/app/routes/brand_rules.py:13)
- [文件上传的图片变换与 copy 路径](D:/java/DRI/fbr-project/backend/file-service/src/main/java/app/file/service/FileService.java:76)
- [图片默认缩放及 PNG 转 JPG 的透明度处理](D:/java/DRI/fbr-project/backend/file-service/src/main/java/app/file/client/MagickClient.java:45)
- [素材库按 ID 查询及删除行为](D:/java/DRI/fbr-project/backend/product-service/src/main/java/app/product/service/MediaLibraryService.java:203)
- [品牌资料中的图片解析实际按 ID 查询](D:/java/DRI/fbr-project/integration/ai-marketing-integration-service/src/main/java/app/aimarketingintegration/service/RestaurantBrandProfileService.java:108)
- [AM Console 已有上传入口](D:/java/DRI/fbr-project/frontend/am-console-site-interface/src/main/java/app/amconsolesite/api/MediaLibraryAJAXWebService.java:33)
- [已有不入素材库的附件草稿上传实现](D:/java/DRI/fbr-project/frontend/am-console-site/src/main/java/app/amconsolesite/service/ArtifactService.java:39)
- [开发环境的存储前缀与 container 配置](D:/java/DRI/fbr-project/backend/file-service/conf/dev/resources/azure.properties:1)
- [UAT 环境的存储前缀与 container 配置](D:/java/DRI/fbr-project/backend/file-service/conf/uat/resources/azure.properties:1)
- [生产环境的存储前缀与 container 配置](D:/java/DRI/fbr-project/backend/file-service/conf/prod/resources/azure.properties:1)

本文以静态代码分析为主，透明检测补做了内存小样本验证；2026-10-29 另执行了第 10.3 节的 8 个定向模拟测试，并对第 12–13 节文档中的 Java 定义做了临时编译检查。未运行完整测试集、构建正式 Java 项目、访问生产数据库、调用真实模型或做视觉验收。上述可靠性、图片分类和权限建议仍需实现阶段针对性验证。

## 10. 2026-10-29 完整链路复核

本次重新核对 Demo 的前端请求、FastAPI schema、生成与双链路改图、图片处理和测试，以及 Java 的 AM Console、Integration、Media Library、file-service 和 Core AI 客户端。Demo 存在用户的未提交修改，本次按当前工作区代码复核，未覆盖、回退或修改这些代码。

三个 collection、候选独立版本、source_id 指向执行记录、透明图仅查看、单一模型 hardcode、通用规范后端维护，以及 social-media 上传不入素材库等已确认决定继续保留。复核提出的每轮附加参考图与画幅错误处理现均已获确认；其余工程建议不视为用户已批准的新字段或业务规则。

### 10.1 两项迁移行为的复核进展

**1. 已确认保留：后续改图时，每轮最多 5 张附加参考图。**

最新 Demo 的 WholeImageEditRequest 已有 reference_asset_ids，最多 5 张；页面支持上传或粘贴，本轮参考图会同时发给 MODEL 与 AGENT，并进入各分支输入快照。用户已确认正式版保留这项能力。

正式设计已同步到第 4.2、6、8.3 节：修改请求使用可选 reference_file_keys（默认空，最多 5 张），两条链路均接收，并按实际顺序记入 input.images 的 EDIT_REFERENCE；不自动再次附带到后续轮次。保持 ImageInput 的三个顶层字段与三个 collection，不增加输出文件选择参数。本项不再作为待确认问题。

代码依据：[改图请求字段](D:/ai_project/social-media/backend/app/schemas.py:183)、[前端参考图入口](D:/ai_project/social-media/frontend/src/components/WholeChatPanel.tsx:192)、[两条分支的输入构造](D:/ai_project/social-media/backend/app/services/whole_image.py:948)。

**2. 已确认：返回比例明显错误时，判该条执行失败。**

复核时发现 MODEL 使用 ImageOps.pad 补边，整图工作台 AGENT 对相对比例差异超过 2% 的成品报 output_aspect_mismatch，独立 Agent 入口没有同样的严格校验，三者行为存在差异。用户已明确正式版采用统一结果规则：返回图片的比例明显错误时，将该条执行判为失败。

正式设计已同步到第 1.3、5、7.2、8.1 节：对应 ImageVersion 标记 FAILED 并保留错误原因和版本号；另一条链路、其他候选及旧成品不受影响，不自动重试。不能用裁切、拉伸或补边把明显比例错误的结果变成成功；比例一致、仅分辨率不同可缩放。2% 仅作为当前 Demo 代码事实，正式数值容差在技术设计时统一。本项不再作为待确认问题。

代码依据：[MODEL 归一化](D:/ai_project/social-media/backend/app/services/whole_image.py:111)、[Agent 比例检查与补边](D:/ai_project/social-media/backend/app/services/agent_image.py:138)、[整图 Agent 的画布约束](D:/ai_project/social-media/backend/app/services/whole_image.py:774)。

### 10.2 无需重开业务范围，但应补齐的契约

| 项目 | 当前缺口与建议 |
|---|---|
| Agent 输入与快照定义 | 已确认后续 Agent 改图保留最新整图 Demo 的画幅、修改范围、菜品保护与本轮参考图约束；首次直接改素材明确第一张为底图，其余为辅助参考。实际语义文本固化到 input.prompt，input.images 保存同序 role + file_key，发送时才拼 URL。第 11.9 节已补齐各场景输入矩阵，保持 ImageInput 三个字段，不将其误称为完整 HTTP 报文归档；外部实际传输格式仍需接入验证 |
| 完整两层接口 DTO | 第 11 节已补齐 17 个 AJAX 入口、品牌模式与平台组合、首次 GENERATE/EDIT 的字段、候选 V1 返回、列表/详情/版本层级、实际输入示例和两层服务归属。展示 URL 只在 View/BFF 派生。Java 类型、异常映射及客户端适配尚未实现，不将文档中的建议路由描述成现有能力 |
| 商家资料使用范围 | 用户已选择简单方案：无品牌风格时，提示词优化和首次生图不自动注入 RestaurantBrandProfile 的商家事实，所需名称、菜系、定位等由 AM 填写；merchant_id 继续用于归属及权限。通用规范及已选平台规则仍应用。风格名称按视觉标签处理，不能误当真实餐厅名称，不增加商家资料副本或自动门店选择 |
| 成品归一化与透明属性 | 当前补边代码按 RGB / RGBA 模式选择白边 / 透明边，完全不透明的 RGBA 成品也可能因补边变成透明，已用内存样本复现。正式实现按已确认规则先拒绝明显错误比例，再处理合格结果的像素尺寸及微小取整差异；处理过程应保持成品的透明属性，is_transparent 以最终保存文件为准，不更改透明图仅查看的既定规则 |
| Agent 最终产物提取 | 最新 Demo 优先使用最终回复中的图片链接，避免把 artifacts 的中间产物当成另一张成品。Java AgentService 保留了 output，但 artifact 投影只有 fileId；需要把最终交付提取、文件下载和中间图排除规则补入适配，不能直接把所有 artifacts 都写入 output.files。用户确认的“一透明一不透明”是交付约定，不代表所有工具中间产物也只有两张 |
| 文件格式、保存与授权 | social-media 路径示例固定 .png，但普通 file-service 上传会处理图片；需明确 PNG 输出的实际编码，选择保留尺寸和 alpha 的保存路径，并校验最终字节。Media Library 图片按 file_key 的解析接口、social-media 目录校验和历史引用校验分开；CurrentSession 的商家权限也要在上传/查看/修改/下载逐项执行，不能把搜索列表过滤当成完整授权 |
| 异步输入交接和恢复 | size 等不进入 ImageInput 的执行参数不能只留在进程内或依赖调用日志重建，应通过可恢复的执行上下文交接。Agent 触发拿到 run_id 后应及时保存，执行期限内轮询同一 run；超时即 FAILED 已确认，触发响应丢失无法取得结果也按失败反馈，不增加 UNKNOWN 状态。首次失败 V1 的人工重新生成复用原输入，并隔离迟到结果、旧回调和重复点击；不借用营销 Task 记录承载图片执行 |

相关代码：[当前 Agent 输入构造](D:/ai_project/social-media/backend/app/services/whole_image.py:774)、[Demo 最终交付提取](D:/ai_project/social-media/backend/app/services/core_ai.py:318)、[Java Agent 结果投影](D:/java/DRI/fbr-project/library/core-ai-library/src/main/java/app/coreai/service/AgentService.java:80)、[Java 图片保存路径](D:/java/DRI/fbr-project/backend/file-service/src/main/java/app/file/service/FileService.java:76)。

透明图“仅查看”按产品功能约定落实为不提供下载操作和不接受其作为编辑来源；本次未把它扩展为浏览器层面的防保存或防复制能力。

### 10.3 本次验证及边界

- 运行 Demo 现有的定向模拟测试，共 8 个测试用例通过，覆盖本轮参考图同时进入 MODEL / AGENT、修改保护输入、Agent 错误画幅拒绝，以及最终回复图片优先于中间 artifacts 等行为；没有付费生图调用。
- 使用内存小图片验证补边行为：不透明 RGB 补白边后仍不透明；完全不透明的 RGBA 补透明边后会变成含透明像素的图片。未生成或查看大图。
- 以上验证确认当前 Demo 的局部行为，不代表完整测试集、正式 Java 接口、Azure 存储链路或真实模型输出已验收。正式项目本次仅静态核对，未执行构建或修改代码。

## 11. 图片功能接口契约整理（2026-10-29）

本节将已确认的业务规则落实为可审阅的接口建议，供 AM Console 与 Integration 对接。路由、字段名、响应投影和分页默认值是本次整理的建议，不代表这些 Java 接口已经存在。继续使用现有三个 collection，不写业务代码，不新增实施计划。

### 11.1 总体约定与接口清单

- AJAX JSON 字段统一 snake_case，Java DTO 字段按最新确认采用 camelCase，通过 @Property 映射；枚举使用稳定 Property 值，比例枚举映射为 1:1 等实际比值。Request/Response/View 分层沿用正式项目。
- POST 创建风格及上传成功返回 201；首次创作、后续改图、失败 V1 重新生成返回 202，表示执行记录已经可靠受理；普通查询/更新返回 200；删除风格返回 204。
- JSON 直接返回业务对象，不新增通用 success/data 包装。分页沿用 skip/limit，建议默认 0/20、limit 范围 1–100，返回 total 和有意义的列表字段。
- 可选 String 缺省/null 表示未填，实际传入空白字符串由 @NotBlank 拒绝；调用方序列化时将未填的可选输入转为 null。默认空列表使用字段初始值 List.of()，显式 null 由 @NotNull 拒绝。注解校验与跨字段业务校验分开。
- 前端只传 file_key，不传临时 URL、图片 role、用户 ID、模型、Agent ID、endpoint、状态或输出。实际角色和执行链路由后端确定。只有上传接口传图片 base64。
- 当前用户由 AM Console 的 CurrentSession 取得；新建时传 merchant_id，查看或修改既有对象时从父对象解析 merchant，再校验访问范围。
- 下表共 17 个新增 AJAX 入口。商家与素材库查询继续复用现有接口，未计入新增数。

| 编号 | 方法与路径 | 用途 | 成功响应 |
|---|---|---|---|
| A01 | GET /ajax/image-option | 平台、用途、画幅和数量限制 | 200：选项 |
| A02 | POST /ajax/media-library/social-media/upload | 上传本功能使用的图片，不入素材库 | 201：file_key、url |
| A03 | PUT /ajax/restaurant-brand-style/search | 商家风格列表 | 200：total、brand_styles |
| A04 | GET /ajax/restaurant-brand-style/:id | 风格详情 | 200：brand_style |
| A05 | POST /ajax/restaurant-brand-style | 新建风格 | 201：brand_style |
| A06 | PUT /ajax/restaurant-brand-style/:id | 全量更新风格 | 200：brand_style |
| A07 | DELETE /ajax/restaurant-brand-style/:id | 删除风格关联记录 | 204 |
| A08 | POST /ajax/restaurant-brand-style/preview | 预览未保存的风格规范文本 | 200：prompt |
| A09 | POST /ajax/image-prompt/optimize | 优化首次整图生成的提示词 | 200：prompt |
| A10 | POST /ajax/image-design | 创建一项创作及各候选 V1 | 202：design_id、candidates |
| A11 | PUT /ajax/image-design/search | 商家创作历史 | 200：total、designs |
| A12 | GET /ajax/image-design/:id | 创作详情与候选运行摘要 | 200：design |
| A13 | GET /ajax/image-design/:id/candidate/:candidate_id/version | 分页查看某候选的版本 | 200：total、versions |
| A14 | GET /ajax/image-design/:id/candidate/:candidate_id/version/:version | 一个逻辑版本的执行详情及实际输入 | 200：design_id、candidate_id、version、status、records |
| A15 | POST /ajax/image-design/:id/candidate/:candidate_id/edit | 基于指定结果继续改图 | 202：design_id、candidate_id、新 version、records |
| A16 | POST /ajax/image-design/:id/candidate/:candidate_id/version/1/regenerate | 手动重新生成失败的首次 V1 | 202：原 design_id、candidate_id、version=1、records |
| A17 | GET /ajax/image-design/:id/candidate/:candidate_id/version/:version/record/:record_id/download | 下载指定执行记录的唯一不透明成品 | 200：图片文件响应 |

### 11.2 A01：页面选项

`GET /ajax/image-option` 不调用模型。响应字段建议：

| 字段 | 类型/内容 |
|---|---|
| platforms | 有序列表；每项 platform、name、default_placement、placements、content_rules |
| platforms[].placements | 每项 placement、name、ratio、width、height、safe_area；safe_area 为 top/right/bottom/left 的 0–1 比例 |
| ratios | 无平台时可选的 1:1、9:16、4:5、16:9、3:4、4:3 |
| default_ratio | 9:16，沿用 Demo 默认值 |
| candidate_count | min=1、max=6、default=3 |
| limits | content_images=6、selected_logo=1、style_reference_images=3、direct_edit_images=5、edit_reference_images=5、brand_logos=5、brand_references=5、brand_colors=6、prompt_length=5000、edit_instruction_length=1000；max_upload_bytes 由正式后端有效配置提供 |

平台目录按当前 Demo 映射为 Java 枚举值，不另建平台表：

| platform | placement | ratio | 画布 |
|---|---|---|---|
| XIAOHONGSHU | NOTE | 3:4 | 1080×1440 |
| INSTAGRAM | PORTRAIT | 4:5 | 1080×1350 |
| INSTAGRAM | SQUARE | 1:1 | 1080×1080 |
| INSTAGRAM | STORY | 9:16 | 1080×1920 |
| TIKTOK | COVER | 9:16 | 1080×1920 |
| GOOGLE_BUSINESS_PROFILE | UPDATE | 4:3 | 1440×1080 |
| META_ADS | FEED | 4:5 | 1080×1350 |
| META_ADS | STORY | 9:16 | 1080×1920 |
| META_ADS | LANDSCAPE | 16:9 | 1920×1080 |

默认用途依次为 NOTE、PORTRAIT、COVER、UPDATE、FEED；小红书的地址、电话、二维码限制及各用途安全区继续由后端目录提供。无平台画幅尺寸沿用 Demo 的 canvas_size 规则，不使用表中的平台像素尺寸作为所有自由画幅的统一值。尺寸属于调用/展示参数，不加入核心 domain。

### 11.3 A02：上传及既有素材接口复用

上传请求与第 6.1 节一致，包含必填 merchant_id、filename、file_base64。file_base64 为实际图片内容的 base64 字符串，建议不接受 data URL 前缀；服务端验证大小、实际格式及商家权限。

```json
{
  "merchant_id": "merchant_001",
  "filename": "poster.jpg",
  "file_base64": "<图片内容的base64>"
}
```

```json
{
  "file_key": "/fbr-dev/product/media-library/merchant_001/social-media/<uuid>.png",
  "url": "<按现有CDN规则生成的展示URL>"
}
```

路径中的 .png 表示实际 PNG 编码；接收其他支持的图片格式时需真正解码、转为 PNG，保留尺寸及 alpha，不能只改扩展名。支持格式和大小采用正式保存通路实际能力，不照搬 Demo 的任意格式接受范围。保存到 static container 下配置的环境路径，不产生 media_library_id、Task、上传记录或 TTL。

既有 `PUT /ajax/media-library/search` 继续用于素材选择，传 merchant_id、media_library_type=IMAGE、skip、limit 等现有参数；响应 `media_libraries[].key` 在提交新图片接口时映射为 file_key。`GET /ajax/media-library/get/:mediaLibraryId` 用于已有素材详情；原 `POST /ajax/media-library/upload` 仍表示进入素材库，不用于 A02。

新上传文件可用于内容图、Logo、品牌风格参考和改图。服务端自行校验 file_key；前端不需要为了使用该文件先创建素材库记录。

### 11.4 A03–A08：品牌风格

**A03 搜索。** 请求 merchant_id 必填，platform/placement 可选，skip/limit 使用统一分页；placement 必须有 platform 且组合合法。按 updated_time 降序、id 降序稳定排序，返回 `{total, brand_styles}`。每项摘要包含 id、merchant_id、name、summary、platform、placement、colors、logos、references、updated_time，不加载 style 长文本；logos/references 在 AJAX View 中为 `{file_key,url}` 列表。

**A05 创建 / A06 更新。** 创建字段如下；更新使用相同可编辑字段但不接收 merchant_id，由 ID 解析原商家，不支持跨商家移动。PUT 为全量替换表单，缺省的可选数组视为 []，可选文本视为 null；不把 PUT 当作只更新出现字段的 PATCH。

| 字段 | 规则 |
|---|---|
| merchant_id | 仅创建/未保存预览必填 |
| name | 必填，最多 80 字符；不做商家内唯一校验 |
| summary | 必填，最多 500 字符 |
| platform、placement | 均必填，必须是合法组合 |
| colors | 默认 []，最多 6 个 #RRGGBB，统一大写 |
| logos、references | 各默认 []、最多 5 个有序 file_key，去重保序并校验商家归属 |
| style.typography、style.subject_style、style.background | 必填，各最多 4000 字符 |
| style.layout | 最多 4000 字符；缺省使用“重点清晰、层级明确，正文不遮挡关键主体” |
| style.decoration、style.identity | 可选，各最多 4000 字符；空白归一化为 null |
| style.restrictions | 可选，最多 6000 字符；空白归一化为 null |

```json
{
  "merchant_id": "merchant_001",
  "name": "暖色菜品风格",
  "summary": "暖色背景，突出真实菜品照片和清晰标题",
  "platform": "INSTAGRAM",
  "placement": "PORTRAIT",
  "colors": ["#E8C89A", "#30251D"],
  "logos": ["<同商家的Logo文件key>"],
  "references": ["<同商家的风格参考文件key>"],
  "style": {
    "typography": "标题醒目，正文清晰易读",
    "subject_style": "保留真实菜品照片",
    "background": "暖色纯净背景",
    "layout": "菜品为主体，标题与正文分层",
    "decoration": null,
    "identity": null,
    "restrictions": "不编造菜名、价格或餐厅事实"
  }
}
```

A04/A05/A06 统一返回 `{brand_style: RestaurantBrandStyleAJAXView}`。详情 View 包含 id、merchant_id、上述内容、created_by/created_time/updated_by/updated_time；其中 logos/references 由请求的字符串列表投影为 `{file_key,url}` 列表，便于显示。Mongo 和内部持久化对象仍保存字符串 file_key，URL 不落库。更新后 created_* 不变，updated_* 取本次会话用户和服务端时间。

**A07 删除。** 只删除风格记录，不删除其图片文件或 MediaLibrary 记录。历史执行按已固化的 prompt/images 读取和继续修改，不要求原风格仍存在；新的 PRESET 请求引用已删除风格则拒绝。

**A08 预览。** 请求使用创建表单内容，允许尚无风格 ID；返回 `{"prompt":"组装后的品牌风格、平台与通用规范文本"}`。只做规范文本组装，不运行图片模型/图片 Agent，不创建风格、Design 或版本。Demo 现有 preview 就是该语义。

保留“复制风格”的能力，但建议不增加专用复制入口：A04 读取内容，将 logos/references 提取为 file_key，修改名称、平台和用途后调用 A05，产生独立新 ID。Logo/参考图增删统一通过 A02 上传、A06 更新数组完成，不照搬 Demo 的专用 logo/reference 上传与删除路由。

### 11.5 A10：统一创建创作

一个入口覆盖首次生成（GENERATE）和首次直接改素材（EDIT）。operation 不作为可任意组合执行链路的开关：GENERATE 的每个候选 V1 只创建 MODEL，EDIT 的每个候选 V1 只创建 AGENT。

| 公共字段 | 必填/默认 | 说明 |
|---|---|---|
| merchant_id | 必填 | 创作归属 |
| operation | 必填 | GENERATE / EDIT |
| prompt | 必填 | 本次生成要求或首次改图要求，最多 5000 字符 |
| candidate_count | 默认 3，1–6 | 一次创建该数量的稳定候选，各有 V1 |
| request_id | 必填，建议 UUID，最多 128 字符 | 同一次按钮提交/网络重发复用；新的主动操作使用新值 |

**GENERATE 专用字段：**

| 字段 | 必填/默认 | 说明 |
|---|---|---|
| prompt_mode | 默认 RAW | RAW / OPTIMIZED；表示传入 prompt 是否已经是优化后的完整提示词，不表示后台自动运行优化 Agent |
| content_file_keys | 默认 []，最多 6 张 | 内容素材，按请求顺序；保留当前后端无参考图时的纯文本生图能力 |
| brand_mode | 默认 NONE | PRESET / REFERENCE / NONE |
| brand_style_id | PRESET 时必填 | 当前 merchant 的品牌风格 ID |
| logo_file_key | 可选，最多一张 | 本次明确选定的 Logo；省略则不自动附带风格的全部 Logo |
| style_reference_file_keys | REFERENCE 时使用，最多 3 张 | 本次仅用于风格的图片，不能冒充内容图 |
| platform、placement | 可选，按下述规则解析 | 采用第 11.2 节稳定枚举 |
| ratio | 无平台时默认 9:16 | 可选自由画幅；有平台时必须与用途预设一致 |

品牌模式与画布的组合校验：

| 模式 | 风格输入 | 平台与画幅 |
|---|---|---|
| PRESET | brand_style_id 必填；style_reference_file_keys 必须为空，使用该风格的参考图 | 省略 platform/placement 时取风格绑定值；显式传入必须与风格一致；ratio 如传也必须一致 |
| REFERENCE | brand_style_id 为空；style_reference_file_keys 建议要求 1–3 张 | platform 可省略；有 platform 时 placement 缺省取目录默认用途，ratio 按用途解析 |
| NONE | brand_style_id 为空、style_reference_file_keys 为空 | 无 platform 时 placement 必须为空，使用 ratio；不自动补入商家资料 |

组合冲突直接拒绝，不默默更换品牌风格或画幅。Logo 和内容图可与上述三种模式组合，但都要校验商家归属。图像角色不是互斥的文件类型，同一文件若承担不同角色，后端需保留实际发送时的角色说明，不能跨角色随意去重导致顺序错位。

```json
{
  "merchant_id": "merchant_001",
  "operation": "GENERATE",
  "prompt": "制作午市套餐海报，标题为工作日午市套餐",
  "prompt_mode": "RAW",
  "content_file_keys": ["<菜品文件key>"],
  "brand_mode": "PRESET",
  "brand_style_id": "style_001",
  "logo_file_key": "<选定Logo文件key>",
  "candidate_count": 3,
  "request_id": "<本次提交的UUID>"
}
```

OPTIMIZED 模式沿用 Demo 的输入差异：优化器已把观察到的风格写进完整提示词，生图时不再次拼品牌长文本或发送 STYLE_REFERENCE 图片；仍发送 CONTENT、选定 LOGO，并追加必要的图片角色、画布、通用约束。RAW 模式由后端组装风格正文，并发送实际选用的 STYLE_REFERENCE。两者最终实际 prompt 和实际图片列表分别保存到各条 ImageVersion.input，不保存 prompt_mode。

调用方使用 OPTIMIZED 时应确保完整提示词对应当前所选素材、风格和平台；改变这些选择后应重新优化或改用 RAW。后端不会通过额外的优化历史表猜测提示词是否已失效。

**EDIT 专用字段：** image_file_keys 必填，有序 1–5 张。第一张为 EDIT_SOURCE，后续 0–4 张为 EDIT_REFERENCE；目标画布取第一张底图。首次没有来源版本，source_id 为空，由后端保存图片角色并明确告诉 Agent 各图用途。GENERATE 的品牌、平台、ratio、prompt_mode、content_file_keys 等字段在该入口不使用，建议传入时直接校验为参数组合不合法；后续已生成图片改图使用 A15。

```json
{
  "merchant_id": "merchant_001",
  "operation": "EDIT",
  "prompt": "将第一张海报的Logo换成第二张图的Logo，其他内容保持不变",
  "image_file_keys": ["<现成海报文件key>", "<新Logo文件key>"],
  "candidate_count": 3,
  "request_id": "<本次提交的UUID>"
}
```

A10 返回示意（为精简仅展示一个候选；实际返回全部候选）：

```json
{
  "design_id": "D1",
  "candidates": [
    {
      "candidate_id": "C1",
      "index": 1,
      "version": 1,
      "records": [
        {"id": "ver_101", "execution_type": "MODEL", "status": "QUEUED"}
      ]
    }
  ]
}
```

候选 ID、顺序和记录 ID 在执行前确定；不要求前端生成 candidate_batch_id、candidate_index 或逐张并发创建独立任务。相同 request_id 和相同内容重发返回同一组身份；同一 request_id 携带不同业务参数建议按冲突拒绝，不能偷偷再出一批图。

### 11.6 A09：提示词优化

请求使用 merchant_id，以及 A10 GENERATE 的 prompt、content_file_keys、brand_mode、brand_style_id、logo_file_key、style_reference_file_keys、platform、placement；ratio 建议同步允许传入，让无平台时也能向优化器提供目标画幅。不传 operation、candidate_count、prompt_mode 或图片模型。

prompt 可省略/null，表示基于当前图片及已选风格生成一版提示词；非空时最多 5000 字符。商家归属、品牌模式及平台组合校验与首次生成一致。返回 `{"prompt":"优化后的完整提示词"}`，结果须非空且不超过 5000 字符；失败或超时返回调用错误，不自动调用图片生成。

该接口不创建 ImageDesign/ImageVersion，也不单独新增优化历史表。优化请求本身目前只有常规调用日志；用户将结果提交给 A10 后，真正用于出图的最终 prompt 才进入各条 ImageVersion.input。前端需要把优化结果作为 prompt，并设置 prompt_mode=OPTIMIZED；只填写 RAW 不会由后端自动优化。

### 11.7 A15/A16/A17：后续修改、失败重新生成、下载

**A15 后续修改。** 请求字段固定为下表，不再重新选择品牌、平台、模型或候选数量。

| 字段 | 规则 |
|---|---|
| source_id | 必填，来源 ImageVersion.id；属于路径中的同一 Design 和 candidate，且具有唯一可用不透明成品 |
| instruction | 必填，本轮修改要求，最多 1000 字符 |
| reference_file_keys | 可选，默认 []，最多 5 张；去重保序，仅本轮使用 |
| request_id | 必填，同一次提交重发复用 |

后端继承来源图的精确尺寸。即使从旧 V1 修改，也分配当前候选下新的最大版本号，例如已有 V3 时从 V1 改图创建 V4；两条新记录的 source_id 都指向实际选中的来源记录。接受请求时同时创建本轮 MODEL、AGENT 两条记录，不能等某条完成后再给另一条分配编号。

```json
{
  "source_id": "ver_101",
  "instruction": "把标题放大，用附图的Logo替换当前Logo，其他内容保持不变",
  "reference_file_keys": ["<本轮Logo参考文件key>"],
  "request_id": "<本次修改提交的UUID>"
}
```

```json
{
  "design_id": "D1",
  "candidate_id": "C1",
  "version": 2,
  "records": [
    {"id": "ver_201", "execution_type": "MODEL", "status": "QUEUED"},
    {"id": "ver_202", "execution_type": "AGENT", "status": "QUEUED"}
  ]
}
```

两条链路独立执行、保存输入和结果。已有可用结果后就可以根据它继续修改，不要求另一条链路已结束；不能因为轮询出现新结果而改变本次 source_id。运行中的不同逻辑版本也允许并存。

**A16 失败 V1 重新生成。** 请求只包含 `{"request_id":"<本次重新生成提交的UUID>"}`；只允许该候选首次 V1 已 FAILED 的情况，包含超时失败。返回与 A15 相同的执行响应结构，但 version 固定为 1、records 中复用原记录 ID 和原 execution_type，仍只有首次入口对应的一条记录。

不接收新 prompt/素材/source_id，不重新优化提示词，不读取最新风格重组输入，也不影响其他候选。只保存最近一次执行的状态、错误、输出和 run_id；原 created_by/created_time 不变。QUEUED/RUNNING/SUCCEEDED 的 V1 及后续 V2+ 不允许通过该入口重启；成品可用但透明图缺失的 PARTIAL 也不作为“首次生成失败”覆盖。每次新的主动点击使用新 request_id；旧请求重发不能再次触发新的执行。超时后的旧响应必须隔离。

**A17 下载。** 路径中的 record_id 就是 ImageVersion.id。服务端核实 merchant 权限、Design/candidate/version/record 的完整归属，从 output.files 解析唯一有效不透明成品；返回图片文件与 `Content-Disposition: attachment`。不要求 file_key、output_id 或文件下标参数。只有透明图、成品缺失或未通过验收时拒绝下载。

透明图片的展示 URL 由版本详情提供，不提供透明图产品下载入口。file-service 的下载 JSON/base64 不直接透传给浏览器；BFF 可使用文件响应控制器组织字节响应，Integration 提供经归属校验后的稳定文件引用。

### 11.8 A11–A14：创作历史、详情与版本查询

**先区分三个层级：** Design 表示一项创作，candidate 表示图一/图二等固定候选，version 表示该候选一次提交的逻辑轮次；records 则是这个逻辑版本下的一条或两条 MODEL/AGENT 执行记录。接口不能把 records 的两条结果展示成两个候选或两个版本。

#### A11 创作列表

请求建议：merchant_id 必填；operation 可选 GENERATE/EDIT，按首次入口筛选；status 可选 RUNNING/HAS_OUTPUT/NO_OUTPUT；skip/limit 使用统一分页。operation、status 都是查询字段，不新增对应的 ImageDesign 持久化字段。

```json
{
  "merchant_id": "merchant_001",
  "operation": "GENERATE",
  "status": "HAS_OUTPUT",
  "skip": 0,
  "limit": 20
}
```

返回 `{total, designs}`。每项包含 id、merchant_id、operation、status、has_usable_output、created_by、created_time、updated_time、candidates 摘要；不返回全量版本和 input 长文本。按 updated_time 降序、id 降序排序。列表状态须先聚合/筛选再分页，total 对应筛选后的 Design 数量。

Design 列表状态与执行状态分别命名，避免把“旧版有成品、最新修改失败”误报为整项无可用结果：

| 列表 status | 规则 |
|---|---|
| RUNNING | 任一候选仍有 QUEUED/RUNNING 执行；同时仍可返回已有成品 |
| HAS_OUTPUT | 没有运行中执行，任一历史版本存在有效不透明成品 |
| NO_OUTPUT | 没有运行中执行，所有候选均无有效不透明成品；可能保留可查看的透明图 |

这些是建议的查询 View 枚举，不是 ImageVersion.status，也不写入 Mongo。latest_version 的失败原因仍通过候选摘要表达。

#### A12 创作详情与候选摘要

返回 `{design: ImageDesignAJAXView}`，公共字段与列表一致。candidates 每项建议包含：

| 字段 | 说明 |
|---|---|
| id、index | 稳定候选身份和展示顺序，index 从 1 开始 |
| latest_version | 最高已创建版本的摘要，包含 version、status、has_usable_output、records 摘要；可能运行中或失败 |
| active_versions | 该候选所有尚有 QUEUED/RUNNING 执行的逻辑版本摘要，不假定只有一轮运行中 |
| preview_output | 可空；默认预览的有效不透明成品，包含 candidate_id、version、execution_type、record_id、file_key、url |

默认预览建议取最高版本号中可用的不透明成品；同版本 MODEL 优先于 AGENT，缺少 MODEL 可用成品时取 AGENT。它只作为初始展示默认值，不能替代用户显式选择的 source_id。没有成品时 preview_output=null，透明图仍可在记录输出中查看；不为候选另存“当前选择”。

列表 candidates 可只投影 id、index、latest_version 的 version/status、preview_output，不返回 active_versions 的完整执行记录。详情包含当前/活跃摘要，但不一次加载所有历史版本。

#### A13 候选版本列表

GET 使用 query 参数 `skip=0&limit=20`，返回 `{total, versions}`，按 version 降序分页，total 是逻辑版本数量，不是 ImageVersion 记录条数。每项包含 version、聚合 status、has_usable_output 和 records 摘要；同一 V2 的 MODEL/AGENT 在同一项中。records 按 MODEL、AGENT 固定顺序。

执行摘要建议包括 id、execution_type、status、error、output、started_time、updated_time；output.files 为文件 View，不包含 input 长文本。轮询某轮执行可直接使用 A14，避免重复读取全部历史。

#### A14 一个逻辑版本的完整记录

响应包含 design_id、candidate_id、version、聚合 status、has_usable_output、records。records 每条在摘要上增加 operation、source_id、input、created_by、created_time；id 始终是该 ImageVersion.id。内部 run_id 用于恢复和诊断，不要求 AJAX 暴露给页面。

以下示例仅展示一个已成功的 AGENT 记录；后续 V2 的实际响应还会包含同版本 MODEL 记录：

```json
{
  "design_id": "D1",
  "candidate_id": "C1",
  "version": 2,
  "status": "SUCCEEDED",
  "has_usable_output": true,
  "records": [
    {
      "id": "ver_202",
      "execution_type": "AGENT",
      "operation": "EDIT",
      "status": "SUCCEEDED",
      "source_id": "ver_101",
      "input": {
        "prompt": "<本轮实际采用的画布要求、保护规则、图片角色说明与用户修改要求>",
        "images": [
          {"role": "EDIT_SOURCE", "file_key": "<选定成品文件key>"},
          {"role": "EDIT_REFERENCE", "file_key": "<本轮Logo参考文件key>"}
        ],
        "brand_style_id": "style_001"
      },
      "output": {
        "files": [
          {"file_key": "<透明图文件key>", "is_transparent": true, "url": "<展示URL>"},
          {"file_key": "<不透明成品文件key>", "is_transparent": false, "url": "<展示URL>"}
        ]
      },
      "error": null,
      "started_time": "2026-10-29T10:00:00+08:00",
      "updated_time": "2026-10-29T10:00:30+08:00",
      "created_by": "<创建用户ID>",
      "created_time": "2026-10-29T09:59:59+08:00"
    }
  ]
}
```

AJAX output.files 的 url 是展示投影；domain 的 ImageOutputFile 仍只有 file_key、is_transparent。input.images 保持稳定的 role/file_key，不把解析出的 URL 写回历史 input。brand_style_id 若从来源链继承，表示原已选风格关联，不表示本轮重新读取最新风格正文；首次直接 EDIT 未选风格时为空。

#### 执行及逻辑版本状态的完整口径

| 单条执行情况 | status 与可用性 |
|---|---|
| 已受理未开始 / 正在执行 | QUEUED / RUNNING；可返回已保存输出 |
| MODEL 成品有效且已保存 | SUCCEEDED，可下载、可作为来源 |
| AGENT 的一透明一不透明均有效且已保存 | SUCCEEDED，不透明成品可下载、可作为来源 |
| AGENT 成品有效且已保存，但透明图获取/保存失败 | 建议 PARTIAL，保留失败原因和成品，成品可继续使用 |
| 缺少有效不透明成品，仅有透明图 | 建议 FAILED，透明图保留查看，不可下载或作为来源；首次 V1 可人工重新生成 |
| 返回多个不透明成品、无法解析有效交付、比例明显错误或执行超时 | FAILED，保留错误原因；不选任意一张/补边后伪装成功，不自动重试 |

仅通过验收并保存成功的文件进入可展示 output；比例错误的成品不进入可用成品集合。is_transparent 依据最终存储字节检测，文件后缀和数组位置不决定角色。超时失败后的迟到结果不再自动回写成功。

逻辑版本状态按 records 汇总：全部 QUEUED 为 QUEUED；存在 RUNNING 或尚有 QUEUED 且已有其他执行启动/结束时为 RUNNING；全部 SUCCEEDED 为 SUCCEEDED；全部 FAILED 为 FAILED；其他已结束组合为 PARTIAL。has_usable_output 独立按有效不透明成品计算。Design 列表状态再按运行中记录与全历史可用成品汇总，不能复用“最新一条执行状态”。

### 11.9 前端业务请求、输入快照与实际外部调用

前端不直接构造 ImageInput。后端先校验请求、解析选项和历史来源、固化实际语义输入，再调固定图片模型或图片 Agent。

| 场景 | 执行路径 | 实际图片顺序与角色 | prompt 内容 |
|---|---|---|---|
| 首次 GENERATE / RAW | MODEL | CONTENT → 选定 LOGO → 实际使用的 STYLE_REFERENCE | 通用规则、角色、所选风格、平台/画布与用户要求 |
| 首次 GENERATE / OPTIMIZED | MODEL | CONTENT → 选定 LOGO | 已优化提示词，以及实际追加的角色、通用与平台/画布规则；不重复拼风格长文本 |
| 首次直接 EDIT | AGENT | 第一张 EDIT_SOURCE → 其余 EDIT_REFERENCE | 用户要求、首图底图/其余参考的角色说明、目标画布与实际采用的保护规则 |
| 后续 EDIT 的 MODEL | MODEL | 沿来源链找到的原 CONTENT → 选定 LOGO → 当前 EDIT_SOURCE → 本轮 EDIT_REFERENCE | 当前图为底图、原素材核对、修改保护及用户要求 |
| 后续 EDIT 的 AGENT | AGENT | 当前 EDIT_SOURCE → 本轮 EDIT_REFERENCE | 画布、局部修改与菜品保护、参考图说明及用户要求 |
| 首次失败 V1 人工重新生成 | 原路径 | 复用原记录已保存的有序图片 | 复用原最终 prompt，不重新优化或加载新风格 |

首次直接 EDIT 的来源链若没有 CONTENT/LOGO，后续 MODEL 不补造角色，也不把第一轮辅助图自动升级为持续携带的 LOGO。本轮参考图默认不会在下一轮重新附带。所有图片引用在真正调用前仍需核实文件存在与当前商家授权。

原始 CONTENT/LOGO 仅作细节核对，当前 EDIT_SOURCE 才是编辑底图；先前用户已经修改的内容应随当前图保留。例如上一轮已更换 Logo，本轮不能因仍附初次 Logo 而自动换回原标识。该优先关系写入实际 prompt，不另增一份“当前素材”状态。

Agent 外部 trigger 仍是单个 input 字符串，建议按如下结构发送；快照中的 prompt 不包含下面的临时 URL 部分：

```text
<ImageInput.prompt：本轮实际规则及用户要求>

图片 1 [EDIT_SOURCE]：<发送时解析的可访问URL>
图片 2 [EDIT_REFERENCE]：<发送时解析的可访问URL>
```

现有 Java AgentService 的 triggerById / triggerResult 可以复用；客户端没有让前端自由传 attachments 的必要。MODEL 适配器按同一快照顺序生成网关支持的图片字段，有图时可调用 /images/edits、无图时 /images/generations；业务 operation 仍保持 GENERATE 或 EDIT。临时 URL、base64、endpoint、模型值和传输参数不成为 ImageInput 新字段。

### 11.10 两层 Java 接口与依赖服务归属

| 入口/能力 | AM Console | Integration / 其他服务 |
|---|---|---|
| A01 | 建议 ImageOptionAJAXWebService | ImageOptionWebService：GET /image-option，维护统一目录 |
| A02 | 在现有 MediaLibraryAJAXWebService 增加独立 uploadSocialMedia 方法 | 直接走 file-service 的保真保存通路；不调 product-service 的素材入库上传 |
| A03–A08 | 建议 RestaurantBrandStyleAJAXWebService | RestaurantBrandStyleWebService，去掉 /ajax 前缀后的同义路由 |
| A09 | 建议 ImagePromptAJAXWebService | ImagePromptWebService：POST /image-prompt/optimize |
| A10–A16 | 建议 ImageDesignAJAXWebService | ImageDesignWebService，去掉 /ajax 前缀后的同义路由，负责 Mongo、编排、验收、超时和恢复 |
| A17 | 文件下载控制器或框架支持的文件响应 | 内部同义下载路由返回经授权的 {file_key} JSON，不把 public 文件响应类型照搬到 Integration JSON DTO |
| 商家选择/已有素材选择 | 复用 MerchantAJAXWebService、MediaLibraryAJAXWebService | 继续使用现有商家权限与 product-service 素材数据 |

外部 DTO 建议位于 am-console-site-interface，命名 CreateImageDesignAJAXRequest、CreateImageDesignAJAXResponse、ImageVersionAJAXView 等；内部 DTO 位于 ai-marketing-integration-service-interface，使用不带 AJAX 的 Request/Response/View。两层不直接将 Mongo domain 当作响应。共享值对象用顶级 Java 类。

操作人建议通过内部专用 operator_id 传递，AM Console 从 CurrentSession 填充，AJAX 不接受该字段；内部写请求放 body，GET 的鉴权上下文可用内部查询 DTO。Integration 根据可信操作人 ID 校验角色和商家范围，不能仅相信前端传来的 merchant_id，也不允许客户端自报管理员角色。创建/更新记录的用户和时间都由后端生成。

现有 product-service `GET /media-library/:mediaLibraryId` 和 `PUT /media-library/list` 不能直接宣称已经提供 merchant + file_key 的批量归属解析。建议补一个仅内部使用的 `PUT /media-library/resolve-by-file-key`：请求 merchant_id、file_keys，返回匹配的素材 id、key、type、format；调用方核对每个新选 key 均匹配且为图片。这个依赖接口不是新的素材库或新 collection，也不增加 AJAX 入口。

解析分三路：新选素材通过 MediaLibrary 归属解析；social-media 上传通过授权的环境/商家命名空间、存在性及真实图像校验；既有版本的历史输入和输出通过父 Design 及已保存快照核验。历史读取不能强制要求当前仍有品牌风格/MediaLibrary 记录，但实际文件缺失必须反馈错误。

请求幂等、候选内原子版本编号、可靠执行上下文、超时后的旧响应隔离需要在实现时完成；本节不假设现有营销 Task 编排已经提供这些能力，也不增加 Task、重试历史表或进度字段。同一请求重发和 AM 主动发起新修改是不同语义。

### 11.11 错误、迁移映射与本次边界

同步请求沿用正式项目已有错误响应格式，不自建一套 Demo error 包装。建议稳定错误代码至少覆盖：merchant_not_accessible、brand_style_not_found、image_file_not_found、image_file_not_owned、invalid_image_request、platform_placement_mismatch、source_not_usable、regenerate_not_allowed、request_id_conflict。各 HTTP 状态遵循框架既有异常映射：参数/组合校验错误、不可访问、不存在和冲突分别处理。

异步生成已经返回 202 后，远端调用失败通过查询接口中的 FAILED/PARTIAL 与可读 error 返回，不把查询正常响应改为 HTTP 500。output_aspect_mismatch、execution_timeout、invalid_image_output 等原因写在对应执行记录；error 仍是 String，不为此增加 domain 错误对象。A09 的同步优化超时则作为本次同步调用错误返回。前端轮询 GET 的网络错误只表示查询失败，生成执行是否超时由后台执行期限判定，不通过只读 GET 修改业务状态。

| Demo 当前能力 | 正式接口处理 |
|---|---|
| whole-image/tasks、batches 与 poster-edits/batches | 合并到 A10 的 GENERATE / EDIT，候选内嵌在一个 Design |
| versions、task/batch 查询 | A11–A14，版本按候选分页，记录按逻辑版本分组 |
| version/output select | 不提供服务端选择接口；实际来源由 A15.source_id 显式提交 |
| retry | 收敛为 A16，仅首次失败 V1，保留最近一次结果 |
| brands 的 Logo/参考图上传和移除 | A02 上传，A06 更新关联数组，不新建素材管理接口 |
| brands/duplicate | A04 读取再 A05 创建独立副本，不增加复制专用接口 |
| brands/preview | A08 返回规范文本，绝不隐含一次图片生成 |
| brand-rules 页面及模型选择 | 不迁入管理接口；后端统一维护规则，图片模型硬编码 |
| assets 管理 | 复用正式 Media Library；本功能不另建 assets 表或删除接口 |

本次核对依据为两边当前工作区的 schema、AJAX/内部接口和调用代码，未访问正式数据库或调用模型。文档中的新增接口、按 file_key 解析、异步执行及保真文件保存均尚未实现；上传格式能力、正式限制配置、外部超时阈值和网关兼容性仍需实施阶段验证，不把它们变成额外的业务选项。

本节文档校验：14 个 JSON 示例均可解析；17 个 AJAX 方法/路径组合无重复；9 组平台用途、比例和画布与当前 Demo 目录一致；全文 33 处本地源码链接的文件及行号均有效。仅修改设计稿，未运行 Java 构建或付费模型调用。

## 12. AM Console 接口 Java 定义（供审核）

按最新要求，先列 WebService 接口声明，再列每个入口的 Request/Response，最后展开它们引用的 View 和枚举。第 11 节继续说明业务规则及 JSON 示例，本节是 Java 类型与注解的具体定义。

### 12.1 阅读与注解约定

- Java 字段采用 camelCase，JSON 通过 @Property 映射为 snake_case；AJAX 类型使用 XxxAJAXRequest / XxxAJAXResponse / XxxAJAXView。方法命名沿用 get/search/create/update/delete 等正式项目习惯。
- 必填 String：@NotNull + @NotBlank；选填 String：仅 @NotBlank，允许省略/null，有值时拒绝空白。可选空输入请在调用前转为 null。@NotBlank 本身不意味着必填。
- 枚举、Boolean、数值、对象和 List 只在非空要求时使用 @NotNull，不加 @NotBlank。Boolean 的 false 是有效值。
- 默认值写在字段初始化中，例如 skip=0、limit=20、candidateCount=3、List.of()。带 @NotNull 的默认字段可以省略，但显式 null 不接受。
- @Size 限制字符串长度或列表数量；@Min/@Max 用于数值。List<String> 的元素非空白、HEX 内容及 file_key 商家归属在统一内容/业务校验中处理，不能把 @NotBlank 或 @Pattern 错加在 List 上。
- Query DTO 使用 @QueryParam。路径身份放在 @PathParam，不造空 Request，也不把仅支持 FIELD 的 @NotNull/@NotBlank 写在方法参数上；路径 id 必须有效，version 为正整数。
- 所有 class/enum 均为独立顶级类型，不使用继承或内部类复用字段。代码块省略重复的 package/import；没有在正式项目中创建对应 .java 文件。
- 注解均来自 core.framework.api.validate，JSON 来自 core.framework.api.json.Property；已核对正式项目使用的 Core-NG 9.3.0 缓存源码。下列公共 import 供阅读，实际文件只导入用到的类型，不使用通配符。

```java
import core.framework.api.http.HTTPStatus;
import core.framework.api.json.Property;
import core.framework.api.validate.Max;
import core.framework.api.validate.Min;
import core.framework.api.validate.NotBlank;
import core.framework.api.validate.NotNull;
import core.framework.api.validate.Size;
import core.framework.api.web.service.DELETE;
import core.framework.api.web.service.GET;
import core.framework.api.web.service.POST;
import core.framework.api.web.service.PUT;
import core.framework.api.web.service.Path;
import core.framework.api.web.service.PathParam;
import core.framework.api.web.service.QueryParam;
import core.framework.api.web.service.ResponseStatus;

import java.time.ZonedDateTime;
import java.util.List;
```

建议按现有包结构归入 api.imageoption、api.imagedesign、api.imageprompt、api.restaurantbrandstyle；上传请求/响应放 api.medialibrary。共用图片 DTO 放 api.imagedesign，共用平台枚举放 api.imageoption，跨包显式 import。

### 12.2 先写接口声明

#### ImageOptionAJAXWebService

页面公共选项。

```java
public interface ImageOptionAJAXWebService {
    // A01
    @GET
    @Path("/ajax/image-option")
    GetImageOptionAJAXResponse get();
}
```

#### MediaLibraryAJAXWebService

只列现有接口需要增加的方法，原有方法保留。

```java
public interface MediaLibraryAJAXWebService {
    // A02
    @POST
    @Path("/ajax/media-library/social-media/upload")
    @ResponseStatus(HTTPStatus.CREATED)
    UploadSocialMediaImageAJAXResponse uploadSocialMedia(UploadSocialMediaImageAJAXRequest request);
}
```

#### RestaurantBrandStyleAJAXWebService

商家视觉风格管理。

```java
public interface RestaurantBrandStyleAJAXWebService {
    // A03
    @PUT
    @Path("/ajax/restaurant-brand-style/search")
    SearchRestaurantBrandStyleAJAXResponse search(SearchRestaurantBrandStyleAJAXRequest request);

    // A04
    @GET
    @Path("/ajax/restaurant-brand-style/:id")
    GetRestaurantBrandStyleAJAXResponse get(@PathParam("id") String id);

    // A05
    @POST
    @Path("/ajax/restaurant-brand-style")
    @ResponseStatus(HTTPStatus.CREATED)
    CreateRestaurantBrandStyleAJAXResponse create(CreateRestaurantBrandStyleAJAXRequest request);

    // A06
    @PUT
    @Path("/ajax/restaurant-brand-style/:id")
    UpdateRestaurantBrandStyleAJAXResponse update(@PathParam("id") String id, UpdateRestaurantBrandStyleAJAXRequest request);

    // A07
    @DELETE
    @Path("/ajax/restaurant-brand-style/:id")
    @ResponseStatus(HTTPStatus.NO_CONTENT)
    void delete(@PathParam("id") String id);

    // A08
    @POST
    @Path("/ajax/restaurant-brand-style/preview")
    PreviewRestaurantBrandStyleAJAXResponse preview(PreviewRestaurantBrandStyleAJAXRequest request);
}
```

#### ImagePromptAJAXWebService

提示词优化。

```java
public interface ImagePromptAJAXWebService {
    // A09
    @POST
    @Path("/ajax/image-prompt/optimize")
    OptimizeImagePromptAJAXResponse optimize(OptimizeImagePromptAJAXRequest request);
}
```

#### ImageDesignAJAXWebService

创作、候选版本及改图；下载 A17 单独使用 Controller。

```java
public interface ImageDesignAJAXWebService {
    // A10
    @POST
    @Path("/ajax/image-design")
    @ResponseStatus(HTTPStatus.ACCEPTED)
    CreateImageDesignAJAXResponse create(CreateImageDesignAJAXRequest request);

    // A11
    @PUT
    @Path("/ajax/image-design/search")
    SearchImageDesignAJAXResponse search(SearchImageDesignAJAXRequest request);

    // A12
    @GET
    @Path("/ajax/image-design/:id")
    GetImageDesignAJAXResponse get(@PathParam("id") String id);

    // A13
    @GET
    @Path("/ajax/image-design/:id/candidate/:candidate_id/version")
    ListImageVersionAJAXResponse listVersion(
        @PathParam("id") String id,
        @PathParam("candidate_id") String candidateId,
        ListImageVersionAJAXRequest request
    );

    // A14
    @GET
    @Path("/ajax/image-design/:id/candidate/:candidate_id/version/:version")
    GetImageVersionAJAXResponse getVersion(
        @PathParam("id") String id,
        @PathParam("candidate_id") String candidateId,
        @PathParam("version") Integer version
    );

    // A15
    @POST
    @Path("/ajax/image-design/:id/candidate/:candidate_id/edit")
    @ResponseStatus(HTTPStatus.ACCEPTED)
    EditImageDesignAJAXResponse edit(@PathParam("id") String id, @PathParam("candidate_id") String candidateId, EditImageDesignAJAXRequest request);

    // A16
    @POST
    @Path("/ajax/image-design/:id/candidate/:candidate_id/version/1/regenerate")
    @ResponseStatus(HTTPStatus.ACCEPTED)
    RegenerateImageDesignAJAXResponse regenerate(
        @PathParam("id") String id,
        @PathParam("candidate_id") String candidateId,
        RegenerateImageDesignAJAXRequest request
    );
}
```

#### A17 文件下载入口

该入口返回 PNG 字节，使用 Core-NG Controller 注册，不定义一个实际无法通过 JSON WebService 返回文件的伪 Response DTO。模块中的入口声明如下；Controller 实现不在本轮编写。

```java
http().route(
    HTTPMethod.GET,
    "/ajax/image-design/:id/candidate/:candidate_id/version/:version/record/:record_id/download",
    bind(ImageVersionDownloadController.class)
);
```

HTTPMethod 来自 core.framework.http；Controller 使用 core.framework.web.Controller / Request / Response。无 body、无额外 query；路径 id、candidate_id、version、record_id 标识下载目标。成功 HTTP 200，Content-Type 为 image/png，Content-Disposition 为 attachment；仅能下载该记录唯一有效的不透明成品。

### 12.3 再写各接口 Request / Response


#### A01：页面选项

`GET /ajax/image-option`。

**Request：** 无 body，也没有额外查询 DTO。

**Response：GetImageOptionAJAXResponse**

```java
public class GetImageOptionAJAXResponse {
    // 必返，按后端目录顺序。
    @NotNull
    @Size(min = 1)
    @Property(name = "platforms")
    public List<ImagePlatformOptionAJAXView> platforms = List.of();

    // 必返，无平台可选比例。
    @NotNull
    @Size(min = 1)
    @Property(name = "ratios")
    public List<ImageRatioAJAXView> ratios = List.of();

    // 必返。
    @NotNull
    @Property(name = "default_ratio")
    public ImageRatioAJAXView defaultRatio;

    // 必返。
    @NotNull
    @Property(name = "candidate_count")
    public ImageCandidateCountOptionAJAXView candidateCount;

    // 必返。
    @NotNull
    @Property(name = "limits")
    public ImageLimitAJAXView limits;
}
```

#### A02：上传图片（不入素材库）

`POST /ajax/media-library/social-media/upload`。

**Request：UploadSocialMediaImageAJAXRequest**

```java
public class UploadSocialMediaImageAJAXRequest {
    // 必填。商家 ID。
    @NotNull
    @NotBlank
    @Property(name = "merchant_id")
    public String merchantId;

    // 必填。原始文件名；保存名由后端生成。
    @NotNull
    @NotBlank
    @Property(name = "filename")
    public String filename;

    // 必填。图片 base64 内容；大小和实际格式需校验。
    @NotNull
    @NotBlank
    @Property(name = "file_base64")
    public String fileBase64;
}
```

**Response：UploadSocialMediaImageAJAXResponse**

```java
public class UploadSocialMediaImageAJAXResponse {
    // 必填。上传生成的稳定引用，不是 media_library_id。
    @NotNull
    @NotBlank
    @Property(name = "file_key")
    public String fileKey;

    // 必填。供上传后展示的 URL。
    @NotNull
    @NotBlank
    @Property(name = "url")
    public String url;
}
```

#### A03：品牌风格列表

`PUT /ajax/restaurant-brand-style/search`。

**Request：SearchRestaurantBrandStyleAJAXRequest**

```java
public class SearchRestaurantBrandStyleAJAXRequest {
    // 必填。查询商家。
    @NotNull
    @NotBlank
    @Property(name = "merchant_id")
    public String merchantId;

    // 选填，默认 null。平台过滤。
    @Property(name = "platform")
    public ImagePlatformAJAXView platform;

    // 选填，默认 null。用途过滤；须同时指定平台。
    @Property(name = "placement")
    public ImagePlacementAJAXView placement;

    // 可省略，默认 0。
    @NotNull
    @Min(0)
    @Property(name = "skip")
    public Integer skip = 0;

    // 可省略，默认 20。
    @NotNull
    @Min(1)
    @Max(100)
    @Property(name = "limit")
    public Integer limit = 20;
}
```

**Response：SearchRestaurantBrandStyleAJAXResponse**

```java
public class SearchRestaurantBrandStyleAJAXResponse {
    // 必返，过滤后总数。
    @NotNull
    @Min(0)
    @Property(name = "total")
    public Integer total;

    // 必返，空结果为 []。
    @NotNull
    @Property(name = "brand_styles")
    public List<RestaurantBrandStyleSummaryAJAXView> brandStyles = List.of();
}
```

#### A04：品牌风格详情

`GET /ajax/restaurant-brand-style/:id`。

路径参数：`id`（id，String），由 @PathParam 提供，不重复放进 body。

**Request：** 无 body，也没有额外查询 DTO。

**Response：GetRestaurantBrandStyleAJAXResponse**

```java
public class GetRestaurantBrandStyleAJAXResponse {
    // 必返。
    @NotNull
    @Property(name = "brand_style")
    public RestaurantBrandStyleAJAXView brandStyle;
}
```

#### A05：创建品牌风格

`POST /ajax/restaurant-brand-style`。

**Request：CreateRestaurantBrandStyleAJAXRequest**

```java
public class CreateRestaurantBrandStyleAJAXRequest {
    // 必填。创建归属商家。
    @NotNull
    @NotBlank
    @Property(name = "merchant_id")
    public String merchantId;

    // 必填。风格名称。
    @NotNull
    @NotBlank
    @Size(max = 80)
    @Property(name = "name")
    public String name;

    // 必填。风格定位。
    @NotNull
    @NotBlank
    @Size(max = 500)
    @Property(name = "summary")
    public String summary;

    // 必填，发布平台。
    @NotNull
    @Property(name = "platform")
    public ImagePlatformAJAXView platform;

    // 必填，与平台匹配的用途。
    @NotNull
    @Property(name = "placement")
    public ImagePlacementAJAXView placement;

    // 默认 []。元素为 #RRGGBB，转大写；元素格式另做内容校验。
    @NotNull
    @Size(max = 6)
    @Property(name = "colors")
    public List<String> colors = List.of();

    // 默认 []。有序 Logo file_key；去重保序并校验商家归属。
    @NotNull
    @Size(max = 5)
    @Property(name = "logos")
    public List<String> logos = List.of();

    // 默认 []。有序风格参考 file_key；去重保序并校验商家归属。
    @NotNull
    @Size(max = 5)
    @Property(name = "references")
    public List<String> references = List.of();

    // 必填，七项风格内容。
    @NotNull
    @Property(name = "style")
    public StyleAJAXView style;
}
```

**Response：CreateRestaurantBrandStyleAJAXResponse**

```java
public class CreateRestaurantBrandStyleAJAXResponse {
    // 必返。
    @NotNull
    @Property(name = "brand_style")
    public RestaurantBrandStyleAJAXView brandStyle;
}
```

#### A06：更新品牌风格

`PUT /ajax/restaurant-brand-style/:id`。

路径参数：`id`（id，String），由 @PathParam 提供，不重复放进 body。

**Request：UpdateRestaurantBrandStyleAJAXRequest**

全量替换表单；商家从路径 id 对应记录取得，不接受 merchant_id。

```java
public class UpdateRestaurantBrandStyleAJAXRequest {
    // 必填。风格名称。
    @NotNull
    @NotBlank
    @Size(max = 80)
    @Property(name = "name")
    public String name;

    // 必填。风格定位。
    @NotNull
    @NotBlank
    @Size(max = 500)
    @Property(name = "summary")
    public String summary;

    // 必填，发布平台。
    @NotNull
    @Property(name = "platform")
    public ImagePlatformAJAXView platform;

    // 必填，与平台匹配的用途。
    @NotNull
    @Property(name = "placement")
    public ImagePlacementAJAXView placement;

    // 默认 []。元素为 #RRGGBB，转大写；元素格式另做内容校验。
    @NotNull
    @Size(max = 6)
    @Property(name = "colors")
    public List<String> colors = List.of();

    // 默认 []。有序 Logo file_key；去重保序并校验商家归属。
    @NotNull
    @Size(max = 5)
    @Property(name = "logos")
    public List<String> logos = List.of();

    // 默认 []。有序风格参考 file_key；去重保序并校验商家归属。
    @NotNull
    @Size(max = 5)
    @Property(name = "references")
    public List<String> references = List.of();

    // 必填，七项风格内容。
    @NotNull
    @Property(name = "style")
    public StyleAJAXView style;
}
```

**Response：UpdateRestaurantBrandStyleAJAXResponse**

```java
public class UpdateRestaurantBrandStyleAJAXResponse {
    // 必返。
    @NotNull
    @Property(name = "brand_style")
    public RestaurantBrandStyleAJAXView brandStyle;
}
```

#### A07：删除品牌风格

`DELETE /ajax/restaurant-brand-style/:id`。

路径参数：`id`（id，String），由 @PathParam 提供，不重复放进 body。

**Request：** 无 body，也没有额外查询 DTO。

**Response：** HTTP 204，无 body；Java 返回 void，不创建空 Response 类。

#### A08：预览品牌规范文本

`POST /ajax/restaurant-brand-style/preview`。

**Request：PreviewRestaurantBrandStyleAJAXRequest**

只返回规范文本，不保存风格或创建图片执行。

```java
public class PreviewRestaurantBrandStyleAJAXRequest {
    // 必填。用于风格图片归属校验。
    @NotNull
    @NotBlank
    @Property(name = "merchant_id")
    public String merchantId;

    // 必填。风格名称。
    @NotNull
    @NotBlank
    @Size(max = 80)
    @Property(name = "name")
    public String name;

    // 必填。风格定位。
    @NotNull
    @NotBlank
    @Size(max = 500)
    @Property(name = "summary")
    public String summary;

    // 必填，发布平台。
    @NotNull
    @Property(name = "platform")
    public ImagePlatformAJAXView platform;

    // 必填，与平台匹配的用途。
    @NotNull
    @Property(name = "placement")
    public ImagePlacementAJAXView placement;

    // 默认 []。元素为 #RRGGBB，转大写；元素格式另做内容校验。
    @NotNull
    @Size(max = 6)
    @Property(name = "colors")
    public List<String> colors = List.of();

    // 默认 []。有序 Logo file_key；去重保序并校验商家归属。
    @NotNull
    @Size(max = 5)
    @Property(name = "logos")
    public List<String> logos = List.of();

    // 默认 []。有序风格参考 file_key；去重保序并校验商家归属。
    @NotNull
    @Size(max = 5)
    @Property(name = "references")
    public List<String> references = List.of();

    // 必填，七项风格内容。
    @NotNull
    @Property(name = "style")
    public StyleAJAXView style;
}
```

**Response：PreviewRestaurantBrandStyleAJAXResponse**

```java
public class PreviewRestaurantBrandStyleAJAXResponse {
    // 必填。组装的规范文本，不是生成图片。
    @NotNull
    @NotBlank
    @Property(name = "prompt")
    public String prompt;
}
```

#### A09：提示词优化

`POST /ajax/image-prompt/optimize`。

**Request：OptimizeImagePromptAJAXRequest**

```java
public class OptimizeImagePromptAJAXRequest {
    // 必填。当前商家。
    @NotNull
    @NotBlank
    @Property(name = "merchant_id")
    public String merchantId;

    // 选填，默认 null。省略/null 时根据图片与已选风格生成提示词；空输入应在调用前转 null。
    @NotBlank
    @Size(max = 5000)
    @Property(name = "prompt")
    public String prompt;

    // 默认 []；仅 GENERATE 使用，EDIT 必须为空。
    @NotNull
    @Size(max = 6)
    @Property(name = "content_file_keys")
    public List<String> contentFileKeys = List.of();

    // 可省略，默认 NONE；枚举有值时按模式校验。
    @NotNull
    @Property(name = "brand_mode")
    public ImageBrandModeAJAXView brandMode = ImageBrandModeAJAXView.NONE;

    // 选填，默认 null。PRESET 时条件必填，其他模式必须为空。
    @NotBlank
    @Property(name = "brand_style_id")
    public String brandStyleId;

    // 选填，默认 null。最多选定一张 Logo。
    @NotBlank
    @Property(name = "logo_file_key")
    public String logoFileKey;

    // 默认 []；REFERENCE 模式条件要求 1–3 张，其他模式为空。
    @NotNull
    @Size(max = 3)
    @Property(name = "style_reference_file_keys")
    public List<String> styleReferenceFileKeys = List.of();

    // 选填，默认 null。PRESET 可从风格解析，其他模式可不指定。
    @Property(name = "platform")
    public ImagePlatformAJAXView platform;

    // 选填，默认 null。有平台时可取默认用途；无平台时必须为空。
    @Property(name = "placement")
    public ImagePlacementAJAXView placement;

    // 选填，默认 null。无平台时缺省 9:16；有平台时必须与用途一致。
    @Property(name = "ratio")
    public ImageRatioAJAXView ratio;
}
```

**Response：OptimizeImagePromptAJAXResponse**

```java
public class OptimizeImagePromptAJAXResponse {
    // 必填。优化后的非空完整提示词。
    @NotNull
    @NotBlank
    @Size(max = 5000)
    @Property(name = "prompt")
    public String prompt;
}
```

#### A10：创建创作

`POST /ajax/image-design`。

**Request：CreateImageDesignAJAXRequest**

同一 DTO 覆盖两种首次入口；条件必填不能错误地添加全局 @NotNull。

```java
public class CreateImageDesignAJAXRequest {
    // 必填。创作归属商家。
    @NotNull
    @NotBlank
    @Property(name = "merchant_id")
    public String merchantId;

    // 必填，GENERATE 或 EDIT。
    @NotNull
    @Property(name = "operation")
    public ImageOperationAJAXView operation;

    // 必填。首次生成或直接改素材的要求。
    @NotNull
    @NotBlank
    @Size(max = 5000)
    @Property(name = "prompt")
    public String prompt;

    // 可省略，默认 3。
    @NotNull
    @Min(1)
    @Max(6)
    @Property(name = "candidate_count")
    public Integer candidateCount = 3;

    // 必填。同一次提交重发保持不变。
    @NotNull
    @NotBlank
    @Size(max = 128)
    @Property(name = "request_id")
    public String requestId;

    // 选填，默认 null。仅 GENERATE 缺省解析为 RAW；EDIT 必须不传。
    @Property(name = "prompt_mode")
    public ImagePromptModeAJAXView promptMode;

    // 默认 []；仅 GENERATE 使用，EDIT 必须为空。
    @NotNull
    @Size(max = 6)
    @Property(name = "content_file_keys")
    public List<String> contentFileKeys = List.of();

    // 选填，默认 null。仅 GENERATE 缺省解析为 NONE；EDIT 不传。
    @Property(name = "brand_mode")
    public ImageBrandModeAJAXView brandMode;

    // 选填，默认 null。PRESET 时条件必填，其他模式必须为空。
    @NotBlank
    @Property(name = "brand_style_id")
    public String brandStyleId;

    // 选填，默认 null。最多选定一张 Logo。
    @NotBlank
    @Property(name = "logo_file_key")
    public String logoFileKey;

    // 默认 []；REFERENCE 模式条件要求 1–3 张，其他模式为空。
    @NotNull
    @Size(max = 3)
    @Property(name = "style_reference_file_keys")
    public List<String> styleReferenceFileKeys = List.of();

    // 选填，默认 null。PRESET 可从风格解析，其他模式可不指定。
    @Property(name = "platform")
    public ImagePlatformAJAXView platform;

    // 选填，默认 null。有平台时可取默认用途；无平台时必须为空。
    @Property(name = "placement")
    public ImagePlacementAJAXView placement;

    // 选填，默认 null。无平台时缺省 9:16；有平台时必须与用途一致。
    @Property(name = "ratio")
    public ImageRatioAJAXView ratio;

    // 条件必填：operation=EDIT 时需 1–5 张；GENERATE 时为 null。第一张底图，其余辅助参考。
    @Size(min = 1, max = 5)
    @Property(name = "image_file_keys")
    public List<String> imageFileKeys;
}
```

**Response：CreateImageDesignAJAXResponse**

```java
public class CreateImageDesignAJAXResponse {
    // 必填。本次创作的固定 ID。
    @NotNull
    @NotBlank
    @Property(name = "design_id")
    public String designId;

    // 必返，数量与受理的 candidate_count 一致。
    @NotNull
    @Size(min = 1, max = 6)
    @Property(name = "candidates")
    public List<ImageCandidateAcceptedAJAXView> candidates = List.of();
}
```

#### A11：创作历史列表

`PUT /ajax/image-design/search`。

**Request：SearchImageDesignAJAXRequest**

```java
public class SearchImageDesignAJAXRequest {
    // 必填。查询商家。
    @NotNull
    @NotBlank
    @Property(name = "merchant_id")
    public String merchantId;

    // 选填，默认 null。按首次入口筛选。
    @Property(name = "operation")
    public ImageOperationAJAXView operation;

    // 选填，默认 null。创作列表状态过滤，不是单条执行状态。
    @Property(name = "status")
    public ImageDesignStatusAJAXView status;

    // 可省略，默认 0。
    @NotNull
    @Min(0)
    @Property(name = "skip")
    public Integer skip = 0;

    // 可省略，默认 20。
    @NotNull
    @Min(1)
    @Max(100)
    @Property(name = "limit")
    public Integer limit = 20;
}
```

**Response：SearchImageDesignAJAXResponse**

```java
public class SearchImageDesignAJAXResponse {
    // 必返，过滤后的 Design 总数，不是执行记录条数。
    @NotNull
    @Min(0)
    @Property(name = "total")
    public Integer total;

    // 必返，空结果为 []。
    @NotNull
    @Property(name = "designs")
    public List<ImageDesignSummaryAJAXView> designs = List.of();
}
```

#### A12：创作详情

`GET /ajax/image-design/:id`。

路径参数：`id`（id，String），由 @PathParam 提供，不重复放进 body。

**Request：** 无 body，也没有额外查询 DTO。

**Response：GetImageDesignAJAXResponse**

```java
public class GetImageDesignAJAXResponse {
    // 必返。
    @NotNull
    @Property(name = "design")
    public ImageDesignAJAXView design;
}
```

#### A13：候选版本列表

`GET /ajax/image-design/:id/candidate/:candidate_id/version`。

路径参数：`id`（id，String）、`candidateId`（candidate_id，String），由 @PathParam 提供，不重复放进 body。

**Query Request：ListImageVersionAJAXRequest**

字段使用 @QueryParam，不使用 @Property；Design/candidate 身份由路径传入。

```java
public class ListImageVersionAJAXRequest {
    // Query，可省略，默认 0。
    @NotNull
    @Min(0)
    @QueryParam(name = "skip")
    public Integer skip = 0;

    // Query，可省略，默认 20。
    @NotNull
    @Min(1)
    @Max(100)
    @QueryParam(name = "limit")
    public Integer limit = 20;
}
```

**Response：ListImageVersionAJAXResponse**

```java
public class ListImageVersionAJAXResponse {
    // 必返，该候选的逻辑版本数量。
    @NotNull
    @Min(0)
    @Property(name = "total")
    public Integer total;

    // 必返，按 version 降序，空结果为 []。
    @NotNull
    @Property(name = "versions")
    public List<ImageVersionGroupAJAXView> versions = List.of();
}
```

#### A14：单个逻辑版本详情

`GET /ajax/image-design/:id/candidate/:candidate_id/version/:version`。

路径参数：`id`（id，String）、`candidateId`（candidate_id，String）、`version`（version，Integer），由 @PathParam 提供，不重复放进 body。

**Request：** 无 body，也没有额外查询 DTO。

**Response：GetImageVersionAJAXResponse**

```java
public class GetImageVersionAJAXResponse {
    // 必填。路径 Design ID。
    @NotNull
    @NotBlank
    @Property(name = "design_id")
    public String designId;

    // 必填。路径候选 ID。
    @NotNull
    @NotBlank
    @Property(name = "candidate_id")
    public String candidateId;

    // 必返，路径逻辑版本号。
    @NotNull
    @Min(1)
    @Property(name = "version")
    public Integer version;

    // 必返。
    @NotNull
    @Property(name = "status")
    public ImageExecutionStatusAJAXView status;

    // 必返。
    @NotNull
    @Property(name = "has_usable_output")
    public Boolean hasUsableOutput;

    // 必返，一个逻辑版本的完整执行记录。
    @NotNull
    @Size(min = 1, max = 2)
    @Property(name = "records")
    public List<ImageVersionAJAXView> records = List.of();
}
```

#### A15：继续改图

`POST /ajax/image-design/:id/candidate/:candidate_id/edit`。

路径参数：`id`（id，String）、`candidateId`（candidate_id，String），由 @PathParam 提供，不重复放进 body。

**Request：EditImageDesignAJAXRequest**

```java
public class EditImageDesignAJAXRequest {
    // 必填。所选来源 ImageVersion.id。
    @NotNull
    @NotBlank
    @Property(name = "source_id")
    public String sourceId;

    // 必填。本轮修改要求。
    @NotNull
    @NotBlank
    @Size(max = 1000)
    @Property(name = "instruction")
    public String instruction;

    // 默认 []，仅本轮附加参考，去重保序。
    @NotNull
    @Size(max = 5)
    @Property(name = "reference_file_keys")
    public List<String> referenceFileKeys = List.of();

    // 必填。本次修改的幂等标识。
    @NotNull
    @NotBlank
    @Size(max = 128)
    @Property(name = "request_id")
    public String requestId;
}
```

**Response：EditImageDesignAJAXResponse**

```java
public class EditImageDesignAJAXResponse {
    // 必填。所在 Design。
    @NotNull
    @NotBlank
    @Property(name = "design_id")
    public String designId;

    // 必填。所在候选。
    @NotNull
    @NotBlank
    @Property(name = "candidate_id")
    public String candidateId;

    // 必返，新分配版本号，至少 2。
    @NotNull
    @Min(2)
    @Property(name = "version")
    public Integer version;

    // 必返，MODEL 与 AGENT 各一条。
    @NotNull
    @Size(min = 2, max = 2)
    @Property(name = "records")
    public List<ImageVersionAcceptedAJAXView> records = List.of();
}
```

#### A16：重新生成失败的首次 V1

`POST /ajax/image-design/:id/candidate/:candidate_id/version/1/regenerate`。

路径参数：`id`（id，String）、`candidateId`（candidate_id，String），由 @PathParam 提供，不重复放进 body。

**Request：RegenerateImageDesignAJAXRequest**

```java
public class RegenerateImageDesignAJAXRequest {
    // 必填。本次手动重新生成的幂等标识；不接受新 prompt 或图片。
    @NotNull
    @NotBlank
    @Size(max = 128)
    @Property(name = "request_id")
    public String requestId;
}
```

**Response：RegenerateImageDesignAJAXResponse**

```java
public class RegenerateImageDesignAJAXResponse {
    // 必填。所在 Design。
    @NotNull
    @NotBlank
    @Property(name = "design_id")
    public String designId;

    // 必填。所在候选。
    @NotNull
    @NotBlank
    @Property(name = "candidate_id")
    public String candidateId;

    // 必返，仍为 1。
    @NotNull
    @Min(1)
    @Max(1)
    @Property(name = "version")
    public Integer version;

    // 必返，原 V1 记录 ID 不变；不新增尝试历史。
    @NotNull
    @Size(min = 1, max = 1)
    @Property(name = "records")
    public List<ImageVersionAcceptedAJAXView> records = List.of();
}
```

#### A17：下载不透明成品

Request 无 body；Response 为图片字节，不是 JSON，所以没有 DownloadImageVersionAJAXRequest / Response 空壳类。服务端授权后取得 file_key，再组织文件响应。Integration 的下载引用请求/响应见第 13 节。

### 12.4 Response 引用的 View 完整定义

以下也是 API 契约的一部分；可空字段不添加 @NotNull，稳定的非空字符串全部标 @NotBlank。View 中的 url 仅为 AJAX 展示投影，不写回 domain。

#### StyleAJAXView

用于写请求和详情响应；layout 通过字段初始值提供默认内容，仍使用 @NotNull @NotBlank。

```java
public class StyleAJAXView {
    // 必填。字体要求。
    @NotNull
    @NotBlank
    @Size(max = 4000)
    @Property(name = "typography")
    public String typography;

    // 必填。主体表现。
    @NotNull
    @NotBlank
    @Size(max = 4000)
    @Property(name = "subject_style")
    public String subjectStyle;

    // 必填。背景要求。
    @NotNull
    @NotBlank
    @Size(max = 4000)
    @Property(name = "background")
    public String background;

    // 可省略，默认“重点清晰、层级明确，正文不遮挡关键主体”；显式 null/空白不接受。
    @NotNull
    @NotBlank
    @Size(max = 4000)
    @Property(name = "layout")
    public String layout = "重点清晰、层级明确，正文不遮挡关键主体";

    // 选填，默认 null。装饰要求。
    @NotBlank
    @Size(max = 4000)
    @Property(name = "decoration")
    public String decoration;

    // 选填，默认 null。固定识别元素。
    @NotBlank
    @Size(max = 4000)
    @Property(name = "identity")
    public String identity;

    // 选填，默认 null。专属要求。
    @NotBlank
    @Size(max = 6000)
    @Property(name = "restrictions")
    public String restrictions;
}
```

#### ImageFileAJAXView

```java
public class ImageFileAJAXView {
    // 必填。稳定文件引用。
    @NotNull
    @NotBlank
    @Property(name = "file_key")
    public String fileKey;

    // 必填。本次展示 URL；不落库。
    @NotNull
    @NotBlank
    @Property(name = "url")
    public String url;
}
```

#### RestaurantBrandStyleSummaryAJAXView

```java
public class RestaurantBrandStyleSummaryAJAXView {
    // 必填。风格 ID。
    @NotNull
    @NotBlank
    @Property(name = "id")
    public String id;

    // 必填。归属商家。
    @NotNull
    @NotBlank
    @Property(name = "merchant_id")
    public String merchantId;

    // 必填。风格名称。
    @NotNull
    @NotBlank
    @Size(max = 80)
    @Property(name = "name")
    public String name;

    // 必填。风格定位。
    @NotNull
    @NotBlank
    @Size(max = 500)
    @Property(name = "summary")
    public String summary;

    // 必返。
    @NotNull
    @Property(name = "platform")
    public ImagePlatformAJAXView platform;

    // 必返。
    @NotNull
    @Property(name = "placement")
    public ImagePlacementAJAXView placement;

    // 必返，默认 []；HEX 元素内容已校验。
    @NotNull
    @Size(max = 6)
    @Property(name = "colors")
    public List<String> colors = List.of();

    // 必返，默认 []；与写请求的字符串列表不同。
    @NotNull
    @Size(max = 5)
    @Property(name = "logos")
    public List<ImageFileAJAXView> logos = List.of();

    // 必返，默认 []；与写请求的字符串列表不同。
    @NotNull
    @Size(max = 5)
    @Property(name = "references")
    public List<ImageFileAJAXView> references = List.of();

    // 必返；服务端时间。
    @NotNull
    @Property(name = "updated_time")
    public ZonedDateTime updatedTime;
}
```

#### RestaurantBrandStyleAJAXView

```java
public class RestaurantBrandStyleAJAXView {
    // 必填。风格 ID。
    @NotNull
    @NotBlank
    @Property(name = "id")
    public String id;

    // 必填。归属商家。
    @NotNull
    @NotBlank
    @Property(name = "merchant_id")
    public String merchantId;

    // 必填。风格名称。
    @NotNull
    @NotBlank
    @Size(max = 80)
    @Property(name = "name")
    public String name;

    // 必填。风格定位。
    @NotNull
    @NotBlank
    @Size(max = 500)
    @Property(name = "summary")
    public String summary;

    // 必返。
    @NotNull
    @Property(name = "platform")
    public ImagePlatformAJAXView platform;

    // 必返。
    @NotNull
    @Property(name = "placement")
    public ImagePlacementAJAXView placement;

    // 必返，默认 []；HEX 元素内容已校验。
    @NotNull
    @Size(max = 6)
    @Property(name = "colors")
    public List<String> colors = List.of();

    // 必返，默认 []；与写请求的字符串列表不同。
    @NotNull
    @Size(max = 5)
    @Property(name = "logos")
    public List<ImageFileAJAXView> logos = List.of();

    // 必返，默认 []；与写请求的字符串列表不同。
    @NotNull
    @Size(max = 5)
    @Property(name = "references")
    public List<ImageFileAJAXView> references = List.of();

    // 必返。
    @NotNull
    @Property(name = "style")
    public StyleAJAXView style;

    // 必填。原创建人用户 ID。
    @NotNull
    @NotBlank
    @Property(name = "created_by")
    public String createdBy;

    // 必返；服务端时间。
    @NotNull
    @Property(name = "created_time")
    public ZonedDateTime createdTime;

    // 必填。最近修改人用户 ID。
    @NotNull
    @NotBlank
    @Property(name = "updated_by")
    public String updatedBy;

    // 必返；服务端时间。
    @NotNull
    @Property(name = "updated_time")
    public ZonedDateTime updatedTime;
}
```

#### ImageReferenceAJAXView

```java
public class ImageReferenceAJAXView {
    // 必返。
    @NotNull
    @Property(name = "role")
    public ImageRoleAJAXView role;

    // 必填。该角色实际使用的稳定图片引用。
    @NotNull
    @NotBlank
    @Property(name = "file_key")
    public String fileKey;
}
```

#### ImageInputAJAXView

```java
public class ImageInputAJAXView {
    // 必填。最终实际规则与用户要求；不按原始输入的 5000 字符限制截断。
    @NotNull
    @NotBlank
    @Property(name = "prompt")
    public String prompt;

    // 必返，默认 []；按实际外部调用顺序。
    @NotNull
    @Property(name = "images")
    public List<ImageReferenceAJAXView> images = List.of();

    // 选填，默认 null。使用过的品牌风格关联；无风格时为 null。
    @NotBlank
    @Property(name = "brand_style_id")
    public String brandStyleId;
}
```

#### ImageOutputFileAJAXView

```java
public class ImageOutputFileAJAXView {
    // 必填。保存成功并通过验收的图片引用。
    @NotNull
    @NotBlank
    @Property(name = "file_key")
    public String fileKey;

    // 必返，true/false；false 也必须保留。
    @NotNull
    @Property(name = "is_transparent")
    public Boolean isTransparent;

    // 必填。派生的展示 URL；不写入 domain。
    @NotNull
    @NotBlank
    @Property(name = "url")
    public String url;
}
```

#### ImageOutputAJAXView

```java
public class ImageOutputAJAXView {
    // 必返，默认 []。最多一透明一不透明；MODEL 正常只有一张不透明成品。
    @NotNull
    @Size(max = 2)
    @Property(name = "files")
    public List<ImageOutputFileAJAXView> files = List.of();
}
```

#### ImageVersionAcceptedAJAXView

```java
public class ImageVersionAcceptedAJAXView {
    // 必填。稳定的 ImageVersion.id。
    @NotNull
    @NotBlank
    @Property(name = "id")
    public String id;

    // 必返。
    @NotNull
    @Property(name = "execution_type")
    public ImageExecutionTypeAJAXView executionType;

    // 必返。
    @NotNull
    @Property(name = "status")
    public ImageExecutionStatusAJAXView status;
}
```

#### ImageCandidateAcceptedAJAXView

```java
public class ImageCandidateAcceptedAJAXView {
    // 必填。创建前分配的候选 ID。
    @NotNull
    @NotBlank
    @Property(name = "candidate_id")
    public String candidateId;

    // 必返，图一至图六的展示顺序。
    @NotNull
    @Min(1)
    @Max(6)
    @Property(name = "index")
    public Integer index;

    // 必返，首次固定为 1。
    @NotNull
    @Min(1)
    @Max(1)
    @Property(name = "version")
    public Integer version;

    // 必返；首次只有 MODEL 或 AGENT 一条。
    @NotNull
    @Size(min = 1, max = 1)
    @Property(name = "records")
    public List<ImageVersionAcceptedAJAXView> records = List.of();
}
```

#### ImageVersionSummaryAJAXView

```java
public class ImageVersionSummaryAJAXView {
    // 必填。该条 ImageVersion 的 ID。
    @NotNull
    @NotBlank
    @Property(name = "id")
    public String id;

    // 必返。
    @NotNull
    @Property(name = "execution_type")
    public ImageExecutionTypeAJAXView executionType;

    // 必返。
    @NotNull
    @Property(name = "status")
    public ImageExecutionStatusAJAXView status;

    // 选填，默认 null。无错误时为 null；失败文本不得包含堆栈或密钥。
    @NotBlank
    @Property(name = "error")
    public String error;

    // 必返，尚无文件时 files=[]。
    @NotNull
    @Property(name = "output")
    public ImageOutputAJAXView output;

    // 可空；尚未开始时为 null。
    @Property(name = "started_time")
    public ZonedDateTime startedTime;

    // 必返；服务端时间。
    @NotNull
    @Property(name = "updated_time")
    public ZonedDateTime updatedTime;
}
```

#### ImageVersionAJAXView

```java
public class ImageVersionAJAXView {
    // 必填。该条 ImageVersion 的 ID。
    @NotNull
    @NotBlank
    @Property(name = "id")
    public String id;

    // 必返。
    @NotNull
    @Property(name = "execution_type")
    public ImageExecutionTypeAJAXView executionType;

    // 必返。
    @NotNull
    @Property(name = "status")
    public ImageExecutionStatusAJAXView status;

    // 选填，默认 null。无错误时为 null；失败文本不得包含堆栈或密钥。
    @NotBlank
    @Property(name = "error")
    public String error;

    // 必返，尚无文件时 files=[]。
    @NotNull
    @Property(name = "output")
    public ImageOutputAJAXView output;

    // 可空；尚未开始时为 null。
    @Property(name = "started_time")
    public ZonedDateTime startedTime;

    // 必返；服务端时间。
    @NotNull
    @Property(name = "updated_time")
    public ZonedDateTime updatedTime;

    // 必返。
    @NotNull
    @Property(name = "operation")
    public ImageOperationAJAXView operation;

    // 选填，默认 null。首次执行为 null；后续为实际来源记录 ID。
    @NotBlank
    @Property(name = "source_id")
    public String sourceId;

    // 必返。
    @NotNull
    @Property(name = "input")
    public ImageInputAJAXView input;

    // 必填。原创建人 ID；V1 重新生成不覆盖。
    @NotNull
    @NotBlank
    @Property(name = "created_by")
    public String createdBy;

    // 必返；服务端时间。
    @NotNull
    @Property(name = "created_time")
    public ZonedDateTime createdTime;
}
```

#### ImageVersionGroupAJAXView

```java
public class ImageVersionGroupAJAXView {
    // 必返，逻辑版本号。
    @NotNull
    @Min(1)
    @Property(name = "version")
    public Integer version;

    // 必返。
    @NotNull
    @Property(name = "status")
    public ImageExecutionStatusAJAXView status;

    // 必返。
    @NotNull
    @Property(name = "has_usable_output")
    public Boolean hasUsableOutput;

    // 必返，按 MODEL、AGENT 顺序；同一轮一条或两条。
    @NotNull
    @Size(min = 1, max = 2)
    @Property(name = "records")
    public List<ImageVersionSummaryAJAXView> records = List.of();
}
```

#### ImagePreviewOutputAJAXView

```java
public class ImagePreviewOutputAJAXView {
    // 必填。所属候选。
    @NotNull
    @NotBlank
    @Property(name = "candidate_id")
    public String candidateId;

    // 必返，所属逻辑版本。
    @NotNull
    @Min(1)
    @Property(name = "version")
    public Integer version;

    // 必返。
    @NotNull
    @Property(name = "execution_type")
    public ImageExecutionTypeAJAXView executionType;

    // 必填。选中预览对应的 ImageVersion.id。
    @NotNull
    @NotBlank
    @Property(name = "record_id")
    public String recordId;

    // 必填。唯一有效不透明成品。
    @NotNull
    @NotBlank
    @Property(name = "file_key")
    public String fileKey;

    // 必填。派生的展示 URL。
    @NotNull
    @NotBlank
    @Property(name = "url")
    public String url;
}
```

#### ImageCandidateSummaryAJAXView

用于创作列表；latest_version 可包含最多两条执行摘要，不包含 input 长文本。

```java
public class ImageCandidateSummaryAJAXView {
    // 必填。稳定候选 ID。
    @NotNull
    @NotBlank
    @Property(name = "id")
    public String id;

    // 必返，展示顺序。
    @NotNull
    @Min(1)
    @Max(6)
    @Property(name = "index")
    public Integer index;

    // 必返，最高已创建版本摘要。
    @NotNull
    @Property(name = "latest_version")
    public ImageVersionGroupAJAXView latestVersion;

    // 可空；无有效不透明成品时为 null。
    @Property(name = "preview_output")
    public ImagePreviewOutputAJAXView previewOutput;
}
```

#### ImageCandidateAJAXView

```java
public class ImageCandidateAJAXView {
    // 必填。稳定候选 ID。
    @NotNull
    @NotBlank
    @Property(name = "id")
    public String id;

    // 必返，展示顺序。
    @NotNull
    @Min(1)
    @Max(6)
    @Property(name = "index")
    public Integer index;

    // 必返，最高已创建版本摘要。
    @NotNull
    @Property(name = "latest_version")
    public ImageVersionGroupAJAXView latestVersion;

    // 可空；无有效不透明成品时为 null。
    @Property(name = "preview_output")
    public ImagePreviewOutputAJAXView previewOutput;

    // 必返，默认 []；允许多轮同时执行。
    @NotNull
    @Property(name = "active_versions")
    public List<ImageVersionGroupAJAXView> activeVersions = List.of();
}
```

#### ImageDesignSummaryAJAXView

```java
public class ImageDesignSummaryAJAXView {
    // 必填。Design ID。
    @NotNull
    @NotBlank
    @Property(name = "id")
    public String id;

    // 必填。归属商家。
    @NotNull
    @NotBlank
    @Property(name = "merchant_id")
    public String merchantId;

    // 必返。
    @NotNull
    @Property(name = "operation")
    public ImageOperationAJAXView operation;

    // 必返。
    @NotNull
    @Property(name = "status")
    public ImageDesignStatusAJAXView status;

    // 必返。
    @NotNull
    @Property(name = "has_usable_output")
    public Boolean hasUsableOutput;

    // 必填。创作创建人，不作为独占所有权。
    @NotNull
    @NotBlank
    @Property(name = "created_by")
    public String createdBy;

    // 必返；服务端时间。
    @NotNull
    @Property(name = "created_time")
    public ZonedDateTime createdTime;

    // 必返；服务端时间。
    @NotNull
    @Property(name = "updated_time")
    public ZonedDateTime updatedTime;

    // 必返，按 index 排序；不装载全量历史。
    @NotNull
    @Size(min = 1, max = 6)
    @Property(name = "candidates")
    public List<ImageCandidateSummaryAJAXView> candidates = List.of();
}
```

#### ImageDesignAJAXView

```java
public class ImageDesignAJAXView {
    // 必填。Design ID。
    @NotNull
    @NotBlank
    @Property(name = "id")
    public String id;

    // 必填。归属商家。
    @NotNull
    @NotBlank
    @Property(name = "merchant_id")
    public String merchantId;

    // 必返。
    @NotNull
    @Property(name = "operation")
    public ImageOperationAJAXView operation;

    // 必返。
    @NotNull
    @Property(name = "status")
    public ImageDesignStatusAJAXView status;

    // 必返。
    @NotNull
    @Property(name = "has_usable_output")
    public Boolean hasUsableOutput;

    // 必填。创作创建人，不作为独占所有权。
    @NotNull
    @NotBlank
    @Property(name = "created_by")
    public String createdBy;

    // 必返；服务端时间。
    @NotNull
    @Property(name = "created_time")
    public ZonedDateTime createdTime;

    // 必返；服务端时间。
    @NotNull
    @Property(name = "updated_time")
    public ZonedDateTime updatedTime;

    // 必返，按 index 排序，含活跃摘要。
    @NotNull
    @Size(min = 1, max = 6)
    @Property(name = "candidates")
    public List<ImageCandidateAJAXView> candidates = List.of();
}
```

#### ImageSafeAreaAJAXView

```java
public class ImageSafeAreaAJAXView {
    // 必返，0–1 的边缘预留比例。
    @NotNull
    @Min(0)
    @Max(1)
    @Property(name = "top")
    public Double top;

    // 必返，0–1 的边缘预留比例。
    @NotNull
    @Min(0)
    @Max(1)
    @Property(name = "right")
    public Double right;

    // 必返，0–1 的边缘预留比例。
    @NotNull
    @Min(0)
    @Max(1)
    @Property(name = "bottom")
    public Double bottom;

    // 必返，0–1 的边缘预留比例。
    @NotNull
    @Min(0)
    @Max(1)
    @Property(name = "left")
    public Double left;
}
```

#### ImagePlacementOptionAJAXView

```java
public class ImagePlacementOptionAJAXView {
    // 必返。
    @NotNull
    @Property(name = "placement")
    public ImagePlacementAJAXView placement;

    // 必填。用途显示名称。
    @NotNull
    @NotBlank
    @Property(name = "name")
    public String name;

    // 必返。
    @NotNull
    @Property(name = "ratio")
    public ImageRatioAJAXView ratio;

    // 必返，像素宽度。
    @NotNull
    @Min(1)
    @Property(name = "width")
    public Integer width;

    // 必返，像素高度。
    @NotNull
    @Min(1)
    @Property(name = "height")
    public Integer height;

    // 必返。
    @NotNull
    @Property(name = "safe_area")
    public ImageSafeAreaAJAXView safeArea;
}
```

#### ImagePlatformOptionAJAXView

```java
public class ImagePlatformOptionAJAXView {
    // 必返。
    @NotNull
    @Property(name = "platform")
    public ImagePlatformAJAXView platform;

    // 必填。平台显示名称。
    @NotNull
    @NotBlank
    @Property(name = "name")
    public String name;

    // 必返。
    @NotNull
    @Property(name = "default_placement")
    public ImagePlacementAJAXView defaultPlacement;

    // 必返，至少一种合法用途。
    @NotNull
    @Size(min = 1)
    @Property(name = "placements")
    public List<ImagePlacementOptionAJAXView> placements = List.of();

    // 必返，默认 []；元素必须是非空白规则文本。
    @NotNull
    @Property(name = "content_rules")
    public List<String> contentRules = List.of();
}
```

#### ImageCandidateCountOptionAJAXView

```java
public class ImageCandidateCountOptionAJAXView {
    // 必返，当前为 1。
    @NotNull
    @Min(1)
    @Property(name = "min")
    public Integer min;

    // 必返，当前为 6。
    @NotNull
    @Min(1)
    @Property(name = "max")
    public Integer max;

    // 必返，当前为 3；Java 用 defaultValue 避开关键字。
    @NotNull
    @Min(1)
    @Property(name = "default")
    public Integer defaultValue;
}
```

#### ImageLimitAJAXView

```java
public class ImageLimitAJAXView {
    // 必返，当前为 6。
    @NotNull
    @Min(1)
    @Property(name = "content_images")
    public Integer contentImages;

    // 必返，当前为 1。
    @NotNull
    @Min(1)
    @Property(name = "selected_logo")
    public Integer selectedLogo;

    // 必返，当前为 3。
    @NotNull
    @Min(1)
    @Property(name = "style_reference_images")
    public Integer styleReferenceImages;

    // 必返，当前为 5。
    @NotNull
    @Min(1)
    @Property(name = "direct_edit_images")
    public Integer directEditImages;

    // 必返，当前为 5。
    @NotNull
    @Min(1)
    @Property(name = "edit_reference_images")
    public Integer editReferenceImages;

    // 必返，当前为 5。
    @NotNull
    @Min(1)
    @Property(name = "brand_logos")
    public Integer brandLogos;

    // 必返，当前为 5。
    @NotNull
    @Min(1)
    @Property(name = "brand_references")
    public Integer brandReferences;

    // 必返，当前为 6。
    @NotNull
    @Min(1)
    @Property(name = "brand_colors")
    public Integer brandColors;

    // 必返，当前为 5000。
    @NotNull
    @Min(1)
    @Property(name = "prompt_length")
    public Integer promptLength;

    // 必返，当前为 1000。
    @NotNull
    @Min(1)
    @Property(name = "edit_instruction_length")
    public Integer editInstructionLength;

    // 必返，从正式有效配置读取；文档不虚构线上数值。
    @NotNull
    @Min(1)
    @Property(name = "max_upload_bytes")
    public Long maxUploadBytes;
}
```

### 12.5 枚举完整定义

平台/用途组合仍按第 11.2 节目录校验。比例的 Java 常量使用 RATIO_1_1 等合法标识符，JSON 值仍为 1:1 等比值。

```java
public enum ImagePlatformAJAXView {
    @Property(name = "XIAOHONGSHU")
    XIAOHONGSHU,

    @Property(name = "INSTAGRAM")
    INSTAGRAM,

    @Property(name = "TIKTOK")
    TIKTOK,

    @Property(name = "GOOGLE_BUSINESS_PROFILE")
    GOOGLE_BUSINESS_PROFILE,

    @Property(name = "META_ADS")
    META_ADS;
}
```

```java
public enum ImagePlacementAJAXView {
    @Property(name = "NOTE")
    NOTE,

    @Property(name = "PORTRAIT")
    PORTRAIT,

    @Property(name = "SQUARE")
    SQUARE,

    @Property(name = "STORY")
    STORY,

    @Property(name = "COVER")
    COVER,

    @Property(name = "UPDATE")
    UPDATE,

    @Property(name = "FEED")
    FEED,

    @Property(name = "LANDSCAPE")
    LANDSCAPE;
}
```

```java
public enum ImageRatioAJAXView {
    @Property(name = "1:1")
    RATIO_1_1,

    @Property(name = "9:16")
    RATIO_9_16,

    @Property(name = "4:5")
    RATIO_4_5,

    @Property(name = "16:9")
    RATIO_16_9,

    @Property(name = "3:4")
    RATIO_3_4,

    @Property(name = "4:3")
    RATIO_4_3;
}
```

```java
public enum ImageBrandModeAJAXView {
    @Property(name = "PRESET")
    PRESET,

    @Property(name = "REFERENCE")
    REFERENCE,

    @Property(name = "NONE")
    NONE;
}
```

```java
public enum ImagePromptModeAJAXView {
    @Property(name = "RAW")
    RAW,

    @Property(name = "OPTIMIZED")
    OPTIMIZED;
}
```

```java
public enum ImageOperationAJAXView {
    @Property(name = "GENERATE")
    GENERATE,

    @Property(name = "EDIT")
    EDIT;
}
```

```java
public enum ImageExecutionTypeAJAXView {
    @Property(name = "MODEL")
    MODEL,

    @Property(name = "AGENT")
    AGENT;
}
```

```java
public enum ImageExecutionStatusAJAXView {
    @Property(name = "QUEUED")
    QUEUED,

    @Property(name = "RUNNING")
    RUNNING,

    @Property(name = "SUCCEEDED")
    SUCCEEDED,

    @Property(name = "PARTIAL")
    PARTIAL,

    @Property(name = "FAILED")
    FAILED;
}
```

```java
public enum ImageDesignStatusAJAXView {
    @Property(name = "RUNNING")
    RUNNING,

    @Property(name = "HAS_OUTPUT")
    HAS_OUTPUT,

    @Property(name = "NO_OUTPUT")
    NO_OUTPUT;
}
```

```java
public enum ImageRoleAJAXView {
    @Property(name = "CONTENT")
    CONTENT,

    @Property(name = "LOGO")
    LOGO,

    @Property(name = "STYLE_REFERENCE")
    STYLE_REFERENCE,

    @Property(name = "EDIT_SOURCE")
    EDIT_SOURCE,

    @Property(name = "EDIT_REFERENCE")
    EDIT_REFERENCE;
}
```

## 13. Integration 内部接口 Java 定义（供后端对接审核）

内部 DTO 使用 XxxRequest / XxxResponse / XxxView，不依赖 am-console-site-interface 的 AJAX 类型。业务 JSON 字段同义；operatorId 只由可信 BFF 从 CurrentSession 填入，前端不可指定。内部的列表、创建、读取和下载均校验当前操作人对商家的访问范围。

内部响应不产生展示 URL：品牌 logos/references 返回 List<String> 的稳定 key，图片输出和预览保留 fileKey，由 BFF 投影 URL；ImageVersionView 额外保留可空 runId，用于内部恢复，AJAX 不暴露。上传 A02 由 BFF 直接调用 file-service，不新增 Integration 上传接口。

### 13.1 先写内部接口声明

#### ImageOptionWebService

```java
public interface ImageOptionWebService {
    // A01
    @GET
    @Path("/image-option")
    GetImageOptionResponse get();
}
```

#### RestaurantBrandStyleWebService

```java
public interface RestaurantBrandStyleWebService {
    // A03
    @PUT
    @Path("/restaurant-brand-style/search")
    SearchRestaurantBrandStyleResponse search(SearchRestaurantBrandStyleRequest request);

    // A04
    @GET
    @Path("/restaurant-brand-style/:id")
    GetRestaurantBrandStyleResponse get(@PathParam("id") String id, GetRestaurantBrandStyleRequest request);

    // A05
    @POST
    @Path("/restaurant-brand-style")
    @ResponseStatus(HTTPStatus.CREATED)
    CreateRestaurantBrandStyleResponse create(CreateRestaurantBrandStyleRequest request);

    // A06
    @PUT
    @Path("/restaurant-brand-style/:id")
    UpdateRestaurantBrandStyleResponse update(@PathParam("id") String id, UpdateRestaurantBrandStyleRequest request);

    // A07
    @DELETE
    @Path("/restaurant-brand-style/:id")
    @ResponseStatus(HTTPStatus.NO_CONTENT)
    void delete(@PathParam("id") String id, DeleteRestaurantBrandStyleRequest request);

    // A08
    @POST
    @Path("/restaurant-brand-style/preview")
    PreviewRestaurantBrandStyleResponse preview(PreviewRestaurantBrandStyleRequest request);
}
```

#### ImagePromptWebService

```java
public interface ImagePromptWebService {
    // A09
    @POST
    @Path("/image-prompt/optimize")
    OptimizeImagePromptResponse optimize(OptimizeImagePromptRequest request);
}
```

#### ImageDesignWebService

```java
public interface ImageDesignWebService {
    // A10
    @POST
    @Path("/image-design")
    @ResponseStatus(HTTPStatus.ACCEPTED)
    CreateImageDesignResponse create(CreateImageDesignRequest request);

    // A11
    @PUT
    @Path("/image-design/search")
    SearchImageDesignResponse search(SearchImageDesignRequest request);

    // A12
    @GET
    @Path("/image-design/:id")
    GetImageDesignResponse get(@PathParam("id") String id, GetImageDesignRequest request);

    // A13
    @GET
    @Path("/image-design/:id/candidate/:candidate_id/version")
    ListImageVersionResponse listVersion(@PathParam("id") String id, @PathParam("candidate_id") String candidateId, ListImageVersionRequest request);

    // A14
    @GET
    @Path("/image-design/:id/candidate/:candidate_id/version/:version")
    GetImageVersionResponse getVersion(
        @PathParam("id") String id,
        @PathParam("candidate_id") String candidateId,
        @PathParam("version") Integer version,
        GetImageVersionRequest request
    );

    // A15
    @POST
    @Path("/image-design/:id/candidate/:candidate_id/edit")
    @ResponseStatus(HTTPStatus.ACCEPTED)
    EditImageDesignResponse edit(@PathParam("id") String id, @PathParam("candidate_id") String candidateId, EditImageDesignRequest request);

    // A16
    @POST
    @Path("/image-design/:id/candidate/:candidate_id/version/1/regenerate")
    @ResponseStatus(HTTPStatus.ACCEPTED)
    RegenerateImageDesignResponse regenerate(
        @PathParam("id") String id,
        @PathParam("candidate_id") String candidateId,
        RegenerateImageDesignRequest request
    );

    // A17
    @GET
    @Path("/image-design/:id/candidate/:candidate_id/version/:version/record/:record_id/download")
    GetImageVersionDownloadResponse getDownloadFile(
        @PathParam("id") String id,
        @PathParam("candidate_id") String candidateId,
        @PathParam("version") Integer version,
        @PathParam("record_id") String recordId,
        GetImageVersionDownloadRequest request
    );
}
```

### 13.2 内部 Request / Response 完整定义

GET/DELETE 的 operatorId 放内部 Query DTO，其余写/搜索请求通过 JSON body 传递；路径身份不重复放入 DTO。


#### A01：页面选项

`GET /image-option`。

**Request：** 无 body，也没有额外查询 DTO。

**Response：GetImageOptionResponse**

```java
public class GetImageOptionResponse {
    // 必返，按后端目录顺序。
    @NotNull
    @Size(min = 1)
    @Property(name = "platforms")
    public List<ImagePlatformOptionView> platforms = List.of();

    // 必返，无平台可选比例。
    @NotNull
    @Size(min = 1)
    @Property(name = "ratios")
    public List<ImageRatioView> ratios = List.of();

    // 必返。
    @NotNull
    @Property(name = "default_ratio")
    public ImageRatioView defaultRatio;

    // 必返。
    @NotNull
    @Property(name = "candidate_count")
    public ImageCandidateCountOptionView candidateCount;

    // 必返。
    @NotNull
    @Property(name = "limits")
    public ImageLimitView limits;
}
```

#### A03：品牌风格列表

`PUT /restaurant-brand-style/search`。

**Request：SearchRestaurantBrandStyleRequest**

```java
public class SearchRestaurantBrandStyleRequest {
    // 必填。查询商家。
    @NotNull
    @NotBlank
    @Property(name = "merchant_id")
    public String merchantId;

    // 选填，默认 null。平台过滤。
    @Property(name = "platform")
    public ImagePlatformView platform;

    // 选填，默认 null。用途过滤；须同时指定平台。
    @Property(name = "placement")
    public ImagePlacementView placement;

    // 可省略，默认 0。
    @NotNull
    @Min(0)
    @Property(name = "skip")
    public Integer skip = 0;

    // 可省略，默认 20。
    @NotNull
    @Min(1)
    @Max(100)
    @Property(name = "limit")
    public Integer limit = 20;

    // 必填，仅可信 BFF 从 CurrentSession 填入，AJAX 不接受。
    @NotNull
    @NotBlank
    @Property(name = "operator_id")
    public String operatorId;
}
```

**Response：SearchRestaurantBrandStyleResponse**

```java
public class SearchRestaurantBrandStyleResponse {
    // 必返，过滤后总数。
    @NotNull
    @Min(0)
    @Property(name = "total")
    public Integer total;

    // 必返，空结果为 []。
    @NotNull
    @Property(name = "brand_styles")
    public List<RestaurantBrandStyleSummaryView> brandStyles = List.of();
}
```

#### A04：品牌风格详情

`GET /restaurant-brand-style/:id`。

路径参数：`id`（id，String），由 @PathParam 提供，不重复放进 body。

**Query Request：GetRestaurantBrandStyleRequest**

仅内部鉴权查询参数，资源身份由路径传入。

```java
public class GetRestaurantBrandStyleRequest {
    // 必填，可信 BFF 填入的当前操作人。
    @NotNull
    @NotBlank
    @QueryParam(name = "operator_id")
    public String operatorId;
}
```

**Response：GetRestaurantBrandStyleResponse**

```java
public class GetRestaurantBrandStyleResponse {
    // 必返。
    @NotNull
    @Property(name = "brand_style")
    public RestaurantBrandStyleView brandStyle;
}
```

#### A05：创建品牌风格

`POST /restaurant-brand-style`。

**Request：CreateRestaurantBrandStyleRequest**

```java
public class CreateRestaurantBrandStyleRequest {
    // 必填。创建归属商家。
    @NotNull
    @NotBlank
    @Property(name = "merchant_id")
    public String merchantId;

    // 必填。风格名称。
    @NotNull
    @NotBlank
    @Size(max = 80)
    @Property(name = "name")
    public String name;

    // 必填。风格定位。
    @NotNull
    @NotBlank
    @Size(max = 500)
    @Property(name = "summary")
    public String summary;

    // 必填，发布平台。
    @NotNull
    @Property(name = "platform")
    public ImagePlatformView platform;

    // 必填，与平台匹配的用途。
    @NotNull
    @Property(name = "placement")
    public ImagePlacementView placement;

    // 默认 []。元素为 #RRGGBB，转大写；元素格式另做内容校验。
    @NotNull
    @Size(max = 6)
    @Property(name = "colors")
    public List<String> colors = List.of();

    // 默认 []。有序 Logo file_key；去重保序并校验商家归属。
    @NotNull
    @Size(max = 5)
    @Property(name = "logos")
    public List<String> logos = List.of();

    // 默认 []。有序风格参考 file_key；去重保序并校验商家归属。
    @NotNull
    @Size(max = 5)
    @Property(name = "references")
    public List<String> references = List.of();

    // 必填，七项风格内容。
    @NotNull
    @Property(name = "style")
    public StyleView style;

    // 必填，仅可信 BFF 从 CurrentSession 填入，AJAX 不接受。
    @NotNull
    @NotBlank
    @Property(name = "operator_id")
    public String operatorId;
}
```

**Response：CreateRestaurantBrandStyleResponse**

```java
public class CreateRestaurantBrandStyleResponse {
    // 必返。
    @NotNull
    @Property(name = "brand_style")
    public RestaurantBrandStyleView brandStyle;
}
```

#### A06：更新品牌风格

`PUT /restaurant-brand-style/:id`。

路径参数：`id`（id，String），由 @PathParam 提供，不重复放进 body。

**Request：UpdateRestaurantBrandStyleRequest**

全量替换表单；商家从路径 id 对应记录取得，不接受 merchant_id。

```java
public class UpdateRestaurantBrandStyleRequest {
    // 必填。风格名称。
    @NotNull
    @NotBlank
    @Size(max = 80)
    @Property(name = "name")
    public String name;

    // 必填。风格定位。
    @NotNull
    @NotBlank
    @Size(max = 500)
    @Property(name = "summary")
    public String summary;

    // 必填，发布平台。
    @NotNull
    @Property(name = "platform")
    public ImagePlatformView platform;

    // 必填，与平台匹配的用途。
    @NotNull
    @Property(name = "placement")
    public ImagePlacementView placement;

    // 默认 []。元素为 #RRGGBB，转大写；元素格式另做内容校验。
    @NotNull
    @Size(max = 6)
    @Property(name = "colors")
    public List<String> colors = List.of();

    // 默认 []。有序 Logo file_key；去重保序并校验商家归属。
    @NotNull
    @Size(max = 5)
    @Property(name = "logos")
    public List<String> logos = List.of();

    // 默认 []。有序风格参考 file_key；去重保序并校验商家归属。
    @NotNull
    @Size(max = 5)
    @Property(name = "references")
    public List<String> references = List.of();

    // 必填，七项风格内容。
    @NotNull
    @Property(name = "style")
    public StyleView style;

    // 必填，仅可信 BFF 从 CurrentSession 填入，AJAX 不接受。
    @NotNull
    @NotBlank
    @Property(name = "operator_id")
    public String operatorId;
}
```

**Response：UpdateRestaurantBrandStyleResponse**

```java
public class UpdateRestaurantBrandStyleResponse {
    // 必返。
    @NotNull
    @Property(name = "brand_style")
    public RestaurantBrandStyleView brandStyle;
}
```

#### A07：删除品牌风格

`DELETE /restaurant-brand-style/:id`。

路径参数：`id`（id，String），由 @PathParam 提供，不重复放进 body。

**Query Request：DeleteRestaurantBrandStyleRequest**

仅内部鉴权查询参数，资源身份由路径传入。

```java
public class DeleteRestaurantBrandStyleRequest {
    // 必填，可信 BFF 填入的当前操作人。
    @NotNull
    @NotBlank
    @QueryParam(name = "operator_id")
    public String operatorId;
}
```

**Response：** HTTP 204，无 body；Java 返回 void，不创建空 Response 类。

#### A08：预览品牌规范文本

`POST /restaurant-brand-style/preview`。

**Request：PreviewRestaurantBrandStyleRequest**

只返回规范文本，不保存风格或创建图片执行。

```java
public class PreviewRestaurantBrandStyleRequest {
    // 必填。用于风格图片归属校验。
    @NotNull
    @NotBlank
    @Property(name = "merchant_id")
    public String merchantId;

    // 必填。风格名称。
    @NotNull
    @NotBlank
    @Size(max = 80)
    @Property(name = "name")
    public String name;

    // 必填。风格定位。
    @NotNull
    @NotBlank
    @Size(max = 500)
    @Property(name = "summary")
    public String summary;

    // 必填，发布平台。
    @NotNull
    @Property(name = "platform")
    public ImagePlatformView platform;

    // 必填，与平台匹配的用途。
    @NotNull
    @Property(name = "placement")
    public ImagePlacementView placement;

    // 默认 []。元素为 #RRGGBB，转大写；元素格式另做内容校验。
    @NotNull
    @Size(max = 6)
    @Property(name = "colors")
    public List<String> colors = List.of();

    // 默认 []。有序 Logo file_key；去重保序并校验商家归属。
    @NotNull
    @Size(max = 5)
    @Property(name = "logos")
    public List<String> logos = List.of();

    // 默认 []。有序风格参考 file_key；去重保序并校验商家归属。
    @NotNull
    @Size(max = 5)
    @Property(name = "references")
    public List<String> references = List.of();

    // 必填，七项风格内容。
    @NotNull
    @Property(name = "style")
    public StyleView style;

    // 必填，仅可信 BFF 从 CurrentSession 填入，AJAX 不接受。
    @NotNull
    @NotBlank
    @Property(name = "operator_id")
    public String operatorId;
}
```

**Response：PreviewRestaurantBrandStyleResponse**

```java
public class PreviewRestaurantBrandStyleResponse {
    // 必填。组装的规范文本，不是生成图片。
    @NotNull
    @NotBlank
    @Property(name = "prompt")
    public String prompt;
}
```

#### A09：提示词优化

`POST /image-prompt/optimize`。

**Request：OptimizeImagePromptRequest**

```java
public class OptimizeImagePromptRequest {
    // 必填。当前商家。
    @NotNull
    @NotBlank
    @Property(name = "merchant_id")
    public String merchantId;

    // 选填，默认 null。省略/null 时根据图片与已选风格生成提示词；空输入应在调用前转 null。
    @NotBlank
    @Size(max = 5000)
    @Property(name = "prompt")
    public String prompt;

    // 默认 []；仅 GENERATE 使用，EDIT 必须为空。
    @NotNull
    @Size(max = 6)
    @Property(name = "content_file_keys")
    public List<String> contentFileKeys = List.of();

    // 可省略，默认 NONE；枚举有值时按模式校验。
    @NotNull
    @Property(name = "brand_mode")
    public ImageBrandModeView brandMode = ImageBrandModeView.NONE;

    // 选填，默认 null。PRESET 时条件必填，其他模式必须为空。
    @NotBlank
    @Property(name = "brand_style_id")
    public String brandStyleId;

    // 选填，默认 null。最多选定一张 Logo。
    @NotBlank
    @Property(name = "logo_file_key")
    public String logoFileKey;

    // 默认 []；REFERENCE 模式条件要求 1–3 张，其他模式为空。
    @NotNull
    @Size(max = 3)
    @Property(name = "style_reference_file_keys")
    public List<String> styleReferenceFileKeys = List.of();

    // 选填，默认 null。PRESET 可从风格解析，其他模式可不指定。
    @Property(name = "platform")
    public ImagePlatformView platform;

    // 选填，默认 null。有平台时可取默认用途；无平台时必须为空。
    @Property(name = "placement")
    public ImagePlacementView placement;

    // 选填，默认 null。无平台时缺省 9:16；有平台时必须与用途一致。
    @Property(name = "ratio")
    public ImageRatioView ratio;

    // 必填，仅可信 BFF 从 CurrentSession 填入，AJAX 不接受。
    @NotNull
    @NotBlank
    @Property(name = "operator_id")
    public String operatorId;
}
```

**Response：OptimizeImagePromptResponse**

```java
public class OptimizeImagePromptResponse {
    // 必填。优化后的非空完整提示词。
    @NotNull
    @NotBlank
    @Size(max = 5000)
    @Property(name = "prompt")
    public String prompt;
}
```

#### A10：创建创作

`POST /image-design`。

**Request：CreateImageDesignRequest**

同一 DTO 覆盖两种首次入口；条件必填不能错误地添加全局 @NotNull。

```java
public class CreateImageDesignRequest {
    // 必填。创作归属商家。
    @NotNull
    @NotBlank
    @Property(name = "merchant_id")
    public String merchantId;

    // 必填，GENERATE 或 EDIT。
    @NotNull
    @Property(name = "operation")
    public ImageOperationView operation;

    // 必填。首次生成或直接改素材的要求。
    @NotNull
    @NotBlank
    @Size(max = 5000)
    @Property(name = "prompt")
    public String prompt;

    // 可省略，默认 3。
    @NotNull
    @Min(1)
    @Max(6)
    @Property(name = "candidate_count")
    public Integer candidateCount = 3;

    // 必填。同一次提交重发保持不变。
    @NotNull
    @NotBlank
    @Size(max = 128)
    @Property(name = "request_id")
    public String requestId;

    // 选填，默认 null。仅 GENERATE 缺省解析为 RAW；EDIT 必须不传。
    @Property(name = "prompt_mode")
    public ImagePromptModeView promptMode;

    // 默认 []；仅 GENERATE 使用，EDIT 必须为空。
    @NotNull
    @Size(max = 6)
    @Property(name = "content_file_keys")
    public List<String> contentFileKeys = List.of();

    // 选填，默认 null。仅 GENERATE 缺省解析为 NONE；EDIT 不传。
    @Property(name = "brand_mode")
    public ImageBrandModeView brandMode;

    // 选填，默认 null。PRESET 时条件必填，其他模式必须为空。
    @NotBlank
    @Property(name = "brand_style_id")
    public String brandStyleId;

    // 选填，默认 null。最多选定一张 Logo。
    @NotBlank
    @Property(name = "logo_file_key")
    public String logoFileKey;

    // 默认 []；REFERENCE 模式条件要求 1–3 张，其他模式为空。
    @NotNull
    @Size(max = 3)
    @Property(name = "style_reference_file_keys")
    public List<String> styleReferenceFileKeys = List.of();

    // 选填，默认 null。PRESET 可从风格解析，其他模式可不指定。
    @Property(name = "platform")
    public ImagePlatformView platform;

    // 选填，默认 null。有平台时可取默认用途；无平台时必须为空。
    @Property(name = "placement")
    public ImagePlacementView placement;

    // 选填，默认 null。无平台时缺省 9:16；有平台时必须与用途一致。
    @Property(name = "ratio")
    public ImageRatioView ratio;

    // 条件必填：operation=EDIT 时需 1–5 张；GENERATE 时为 null。第一张底图，其余辅助参考。
    @Size(min = 1, max = 5)
    @Property(name = "image_file_keys")
    public List<String> imageFileKeys;

    // 必填，仅可信 BFF 从 CurrentSession 填入，AJAX 不接受。
    @NotNull
    @NotBlank
    @Property(name = "operator_id")
    public String operatorId;
}
```

**Response：CreateImageDesignResponse**

```java
public class CreateImageDesignResponse {
    // 必填。本次创作的固定 ID。
    @NotNull
    @NotBlank
    @Property(name = "design_id")
    public String designId;

    // 必返，数量与受理的 candidate_count 一致。
    @NotNull
    @Size(min = 1, max = 6)
    @Property(name = "candidates")
    public List<ImageCandidateAcceptedView> candidates = List.of();
}
```

#### A11：创作历史列表

`PUT /image-design/search`。

**Request：SearchImageDesignRequest**

```java
public class SearchImageDesignRequest {
    // 必填。查询商家。
    @NotNull
    @NotBlank
    @Property(name = "merchant_id")
    public String merchantId;

    // 选填，默认 null。按首次入口筛选。
    @Property(name = "operation")
    public ImageOperationView operation;

    // 选填，默认 null。创作列表状态过滤，不是单条执行状态。
    @Property(name = "status")
    public ImageDesignStatusView status;

    // 可省略，默认 0。
    @NotNull
    @Min(0)
    @Property(name = "skip")
    public Integer skip = 0;

    // 可省略，默认 20。
    @NotNull
    @Min(1)
    @Max(100)
    @Property(name = "limit")
    public Integer limit = 20;

    // 必填，仅可信 BFF 从 CurrentSession 填入，AJAX 不接受。
    @NotNull
    @NotBlank
    @Property(name = "operator_id")
    public String operatorId;
}
```

**Response：SearchImageDesignResponse**

```java
public class SearchImageDesignResponse {
    // 必返，过滤后的 Design 总数，不是执行记录条数。
    @NotNull
    @Min(0)
    @Property(name = "total")
    public Integer total;

    // 必返，空结果为 []。
    @NotNull
    @Property(name = "designs")
    public List<ImageDesignSummaryView> designs = List.of();
}
```

#### A12：创作详情

`GET /image-design/:id`。

路径参数：`id`（id，String），由 @PathParam 提供，不重复放进 body。

**Query Request：GetImageDesignRequest**

仅内部鉴权查询参数，资源身份由路径传入。

```java
public class GetImageDesignRequest {
    // 必填，可信 BFF 填入的当前操作人。
    @NotNull
    @NotBlank
    @QueryParam(name = "operator_id")
    public String operatorId;
}
```

**Response：GetImageDesignResponse**

```java
public class GetImageDesignResponse {
    // 必返。
    @NotNull
    @Property(name = "design")
    public ImageDesignView design;
}
```

#### A13：候选版本列表

`GET /image-design/:id/candidate/:candidate_id/version`。

路径参数：`id`（id，String）、`candidateId`（candidate_id，String），由 @PathParam 提供，不重复放进 body。

**Query Request：ListImageVersionRequest**

字段使用 @QueryParam，不使用 @Property；Design/candidate 身份由路径传入。

```java
public class ListImageVersionRequest {
    // Query，可省略，默认 0。
    @NotNull
    @Min(0)
    @QueryParam(name = "skip")
    public Integer skip = 0;

    // Query，可省略，默认 20。
    @NotNull
    @Min(1)
    @Max(100)
    @QueryParam(name = "limit")
    public Integer limit = 20;

    // 必填，仅可信 BFF 从 CurrentSession 填入，AJAX 不接受。
    @NotNull
    @NotBlank
    @QueryParam(name = "operator_id")
    public String operatorId;
}
```

**Response：ListImageVersionResponse**

```java
public class ListImageVersionResponse {
    // 必返，该候选的逻辑版本数量。
    @NotNull
    @Min(0)
    @Property(name = "total")
    public Integer total;

    // 必返，按 version 降序，空结果为 []。
    @NotNull
    @Property(name = "versions")
    public List<ImageVersionGroupView> versions = List.of();
}
```

#### A14：单个逻辑版本详情

`GET /image-design/:id/candidate/:candidate_id/version/:version`。

路径参数：`id`（id，String）、`candidateId`（candidate_id，String）、`version`（version，Integer），由 @PathParam 提供，不重复放进 body。

**Query Request：GetImageVersionRequest**

仅内部鉴权查询参数，资源身份由路径传入。

```java
public class GetImageVersionRequest {
    // 必填，可信 BFF 填入的当前操作人。
    @NotNull
    @NotBlank
    @QueryParam(name = "operator_id")
    public String operatorId;
}
```

**Response：GetImageVersionResponse**

```java
public class GetImageVersionResponse {
    // 必填。路径 Design ID。
    @NotNull
    @NotBlank
    @Property(name = "design_id")
    public String designId;

    // 必填。路径候选 ID。
    @NotNull
    @NotBlank
    @Property(name = "candidate_id")
    public String candidateId;

    // 必返，路径逻辑版本号。
    @NotNull
    @Min(1)
    @Property(name = "version")
    public Integer version;

    // 必返。
    @NotNull
    @Property(name = "status")
    public ImageExecutionStatusView status;

    // 必返。
    @NotNull
    @Property(name = "has_usable_output")
    public Boolean hasUsableOutput;

    // 必返，一个逻辑版本的完整执行记录。
    @NotNull
    @Size(min = 1, max = 2)
    @Property(name = "records")
    public List<ImageVersionView> records = List.of();
}
```

#### A15：继续改图

`POST /image-design/:id/candidate/:candidate_id/edit`。

路径参数：`id`（id，String）、`candidateId`（candidate_id，String），由 @PathParam 提供，不重复放进 body。

**Request：EditImageDesignRequest**

```java
public class EditImageDesignRequest {
    // 必填。所选来源 ImageVersion.id。
    @NotNull
    @NotBlank
    @Property(name = "source_id")
    public String sourceId;

    // 必填。本轮修改要求。
    @NotNull
    @NotBlank
    @Size(max = 1000)
    @Property(name = "instruction")
    public String instruction;

    // 默认 []，仅本轮附加参考，去重保序。
    @NotNull
    @Size(max = 5)
    @Property(name = "reference_file_keys")
    public List<String> referenceFileKeys = List.of();

    // 必填。本次修改的幂等标识。
    @NotNull
    @NotBlank
    @Size(max = 128)
    @Property(name = "request_id")
    public String requestId;

    // 必填，仅可信 BFF 从 CurrentSession 填入，AJAX 不接受。
    @NotNull
    @NotBlank
    @Property(name = "operator_id")
    public String operatorId;
}
```

**Response：EditImageDesignResponse**

```java
public class EditImageDesignResponse {
    // 必填。所在 Design。
    @NotNull
    @NotBlank
    @Property(name = "design_id")
    public String designId;

    // 必填。所在候选。
    @NotNull
    @NotBlank
    @Property(name = "candidate_id")
    public String candidateId;

    // 必返，新分配版本号，至少 2。
    @NotNull
    @Min(2)
    @Property(name = "version")
    public Integer version;

    // 必返，MODEL 与 AGENT 各一条。
    @NotNull
    @Size(min = 2, max = 2)
    @Property(name = "records")
    public List<ImageVersionAcceptedView> records = List.of();
}
```

#### A16：重新生成失败的首次 V1

`POST /image-design/:id/candidate/:candidate_id/version/1/regenerate`。

路径参数：`id`（id，String）、`candidateId`（candidate_id，String），由 @PathParam 提供，不重复放进 body。

**Request：RegenerateImageDesignRequest**

```java
public class RegenerateImageDesignRequest {
    // 必填。本次手动重新生成的幂等标识；不接受新 prompt 或图片。
    @NotNull
    @NotBlank
    @Size(max = 128)
    @Property(name = "request_id")
    public String requestId;

    // 必填，仅可信 BFF 从 CurrentSession 填入，AJAX 不接受。
    @NotNull
    @NotBlank
    @Property(name = "operator_id")
    public String operatorId;
}
```

**Response：RegenerateImageDesignResponse**

```java
public class RegenerateImageDesignResponse {
    // 必填。所在 Design。
    @NotNull
    @NotBlank
    @Property(name = "design_id")
    public String designId;

    // 必填。所在候选。
    @NotNull
    @NotBlank
    @Property(name = "candidate_id")
    public String candidateId;

    // 必返，仍为 1。
    @NotNull
    @Min(1)
    @Max(1)
    @Property(name = "version")
    public Integer version;

    // 必返，原 V1 记录 ID 不变；不新增尝试历史。
    @NotNull
    @Size(min = 1, max = 1)
    @Property(name = "records")
    public List<ImageVersionAcceptedView> records = List.of();
}
```

#### A17：下载不透明成品

`GET /image-design/:id/candidate/:candidate_id/version/:version/record/:record_id/download`。

路径参数：`id`（id，String）、`candidateId`（candidate_id，String）、`version`（version，Integer）、`recordId`（record_id，String），由 @PathParam 提供，不重复放进 body。

**Query Request：GetImageVersionDownloadRequest**

仅内部鉴权查询参数，资源身份由路径传入。

```java
public class GetImageVersionDownloadRequest {
    // 必填，可信 BFF 填入的当前操作人。
    @NotNull
    @NotBlank
    @QueryParam(name = "operator_id")
    public String operatorId;
}
```

**Response：GetImageVersionDownloadResponse**

```java
public class GetImageVersionDownloadResponse {
    // 必返，授权后得到的唯一有效不透明成品。
    @NotNull
    @NotBlank
    @Property(name = "file_key")
    public String fileKey;
}
```

### 13.3 内部 View 完整定义

不直接暴露 Mongo domain；内部 View 中没有 AJAX 的派生 URL。除下列明确差异外，字段语义与第 12 节一致。

#### StyleView

```java
public class StyleView {
    // 必填。字体要求。
    @NotNull
    @NotBlank
    @Size(max = 4000)
    @Property(name = "typography")
    public String typography;

    // 必填。主体表现。
    @NotNull
    @NotBlank
    @Size(max = 4000)
    @Property(name = "subject_style")
    public String subjectStyle;

    // 必填。背景要求。
    @NotNull
    @NotBlank
    @Size(max = 4000)
    @Property(name = "background")
    public String background;

    // 可省略，默认“重点清晰、层级明确，正文不遮挡关键主体”；显式 null/空白不接受。
    @NotNull
    @NotBlank
    @Size(max = 4000)
    @Property(name = "layout")
    public String layout = "重点清晰、层级明确，正文不遮挡关键主体";

    // 选填，默认 null。装饰要求。
    @NotBlank
    @Size(max = 4000)
    @Property(name = "decoration")
    public String decoration;

    // 选填，默认 null。固定识别元素。
    @NotBlank
    @Size(max = 4000)
    @Property(name = "identity")
    public String identity;

    // 选填，默认 null。专属要求。
    @NotBlank
    @Size(max = 6000)
    @Property(name = "restrictions")
    public String restrictions;
}
```

#### RestaurantBrandStyleSummaryView

```java
public class RestaurantBrandStyleSummaryView {
    // 必填。风格 ID。
    @NotNull
    @NotBlank
    @Property(name = "id")
    public String id;

    // 必填。归属商家。
    @NotNull
    @NotBlank
    @Property(name = "merchant_id")
    public String merchantId;

    // 必填。风格名称。
    @NotNull
    @NotBlank
    @Size(max = 80)
    @Property(name = "name")
    public String name;

    // 必填。风格定位。
    @NotNull
    @NotBlank
    @Size(max = 500)
    @Property(name = "summary")
    public String summary;

    // 必返。
    @NotNull
    @Property(name = "platform")
    public ImagePlatformView platform;

    // 必返。
    @NotNull
    @Property(name = "placement")
    public ImagePlacementView placement;

    // 必返，默认 []；HEX 元素内容已校验。
    @NotNull
    @Size(max = 6)
    @Property(name = "colors")
    public List<String> colors = List.of();

    // 必返，默认 []，内部仅返回稳定 file_key，不含展示 URL。
    @NotNull
    @Size(max = 5)
    @Property(name = "logos")
    public List<String> logos = List.of();

    // 必返，默认 []，内部仅返回稳定 file_key，不含展示 URL。
    @NotNull
    @Size(max = 5)
    @Property(name = "references")
    public List<String> references = List.of();

    // 必返；服务端时间。
    @NotNull
    @Property(name = "updated_time")
    public ZonedDateTime updatedTime;
}
```

#### RestaurantBrandStyleView

```java
public class RestaurantBrandStyleView {
    // 必填。风格 ID。
    @NotNull
    @NotBlank
    @Property(name = "id")
    public String id;

    // 必填。归属商家。
    @NotNull
    @NotBlank
    @Property(name = "merchant_id")
    public String merchantId;

    // 必填。风格名称。
    @NotNull
    @NotBlank
    @Size(max = 80)
    @Property(name = "name")
    public String name;

    // 必填。风格定位。
    @NotNull
    @NotBlank
    @Size(max = 500)
    @Property(name = "summary")
    public String summary;

    // 必返。
    @NotNull
    @Property(name = "platform")
    public ImagePlatformView platform;

    // 必返。
    @NotNull
    @Property(name = "placement")
    public ImagePlacementView placement;

    // 必返，默认 []；HEX 元素内容已校验。
    @NotNull
    @Size(max = 6)
    @Property(name = "colors")
    public List<String> colors = List.of();

    // 必返，默认 []，内部仅返回稳定 file_key，不含展示 URL。
    @NotNull
    @Size(max = 5)
    @Property(name = "logos")
    public List<String> logos = List.of();

    // 必返，默认 []，内部仅返回稳定 file_key，不含展示 URL。
    @NotNull
    @Size(max = 5)
    @Property(name = "references")
    public List<String> references = List.of();

    // 必返。
    @NotNull
    @Property(name = "style")
    public StyleView style;

    // 必填。原创建人用户 ID。
    @NotNull
    @NotBlank
    @Property(name = "created_by")
    public String createdBy;

    // 必返；服务端时间。
    @NotNull
    @Property(name = "created_time")
    public ZonedDateTime createdTime;

    // 必填。最近修改人用户 ID。
    @NotNull
    @NotBlank
    @Property(name = "updated_by")
    public String updatedBy;

    // 必返；服务端时间。
    @NotNull
    @Property(name = "updated_time")
    public ZonedDateTime updatedTime;
}
```

#### ImageReferenceView

```java
public class ImageReferenceView {
    // 必返。
    @NotNull
    @Property(name = "role")
    public ImageRoleView role;

    // 必填。该角色实际使用的稳定图片引用。
    @NotNull
    @NotBlank
    @Property(name = "file_key")
    public String fileKey;
}
```

#### ImageInputView

```java
public class ImageInputView {
    // 必填。最终实际规则与用户要求；不按原始输入的 5000 字符限制截断。
    @NotNull
    @NotBlank
    @Property(name = "prompt")
    public String prompt;

    // 必返，默认 []；按实际外部调用顺序。
    @NotNull
    @Property(name = "images")
    public List<ImageReferenceView> images = List.of();

    // 选填，默认 null。使用过的品牌风格关联；无风格时为 null。
    @NotBlank
    @Property(name = "brand_style_id")
    public String brandStyleId;
}
```

#### ImageOutputFileView

```java
public class ImageOutputFileView {
    // 必填。保存成功并通过验收的图片引用。
    @NotNull
    @NotBlank
    @Property(name = "file_key")
    public String fileKey;

    // 必返，true/false；false 也必须保留。
    @NotNull
    @Property(name = "is_transparent")
    public Boolean isTransparent;
}
```

#### ImageOutputView

```java
public class ImageOutputView {
    // 必返，默认 []。最多一透明一不透明；MODEL 正常只有一张不透明成品。
    @NotNull
    @Size(max = 2)
    @Property(name = "files")
    public List<ImageOutputFileView> files = List.of();
}
```

#### ImageVersionAcceptedView

```java
public class ImageVersionAcceptedView {
    // 必填。稳定的 ImageVersion.id。
    @NotNull
    @NotBlank
    @Property(name = "id")
    public String id;

    // 必返。
    @NotNull
    @Property(name = "execution_type")
    public ImageExecutionTypeView executionType;

    // 必返。
    @NotNull
    @Property(name = "status")
    public ImageExecutionStatusView status;
}
```

#### ImageCandidateAcceptedView

```java
public class ImageCandidateAcceptedView {
    // 必填。创建前分配的候选 ID。
    @NotNull
    @NotBlank
    @Property(name = "candidate_id")
    public String candidateId;

    // 必返，图一至图六的展示顺序。
    @NotNull
    @Min(1)
    @Max(6)
    @Property(name = "index")
    public Integer index;

    // 必返，首次固定为 1。
    @NotNull
    @Min(1)
    @Max(1)
    @Property(name = "version")
    public Integer version;

    // 必返；首次只有 MODEL 或 AGENT 一条。
    @NotNull
    @Size(min = 1, max = 1)
    @Property(name = "records")
    public List<ImageVersionAcceptedView> records = List.of();
}
```

#### ImageVersionSummaryView

```java
public class ImageVersionSummaryView {
    // 必填。该条 ImageVersion 的 ID。
    @NotNull
    @NotBlank
    @Property(name = "id")
    public String id;

    // 必返。
    @NotNull
    @Property(name = "execution_type")
    public ImageExecutionTypeView executionType;

    // 必返。
    @NotNull
    @Property(name = "status")
    public ImageExecutionStatusView status;

    // 选填，默认 null。无错误时为 null；失败文本不得包含堆栈或密钥。
    @NotBlank
    @Property(name = "error")
    public String error;

    // 必返，尚无文件时 files=[]。
    @NotNull
    @Property(name = "output")
    public ImageOutputView output;

    // 可空；尚未开始时为 null。
    @Property(name = "started_time")
    public ZonedDateTime startedTime;

    // 必返；服务端时间。
    @NotNull
    @Property(name = "updated_time")
    public ZonedDateTime updatedTime;
}
```

#### ImageVersionView

```java
public class ImageVersionView {
    // 必填。该条 ImageVersion 的 ID。
    @NotNull
    @NotBlank
    @Property(name = "id")
    public String id;

    // 必返。
    @NotNull
    @Property(name = "execution_type")
    public ImageExecutionTypeView executionType;

    // 必返。
    @NotNull
    @Property(name = "status")
    public ImageExecutionStatusView status;

    // 选填，默认 null。无错误时为 null；失败文本不得包含堆栈或密钥。
    @NotBlank
    @Property(name = "error")
    public String error;

    // 必返，尚无文件时 files=[]。
    @NotNull
    @Property(name = "output")
    public ImageOutputView output;

    // 可空；尚未开始时为 null。
    @Property(name = "started_time")
    public ZonedDateTime startedTime;

    // 必返；服务端时间。
    @NotNull
    @Property(name = "updated_time")
    public ZonedDateTime updatedTime;

    // 必返。
    @NotNull
    @Property(name = "operation")
    public ImageOperationView operation;

    // 选填，默认 null。首次执行为 null；后续为实际来源记录 ID。
    @NotBlank
    @Property(name = "source_id")
    public String sourceId;

    // 必返。
    @NotNull
    @Property(name = "input")
    public ImageInputView input;

    // 必填。原创建人 ID；V1 重新生成不覆盖。
    @NotNull
    @NotBlank
    @Property(name = "created_by")
    public String createdBy;

    // 必返；服务端时间。
    @NotNull
    @Property(name = "created_time")
    public ZonedDateTime createdTime;

    // 可空，Agent 运行 ID；MODEL 为 null，仅内部恢复/诊断使用。
    @NotBlank
    @Property(name = "run_id")
    public String runId;
}
```

#### ImageVersionGroupView

```java
public class ImageVersionGroupView {
    // 必返，逻辑版本号。
    @NotNull
    @Min(1)
    @Property(name = "version")
    public Integer version;

    // 必返。
    @NotNull
    @Property(name = "status")
    public ImageExecutionStatusView status;

    // 必返。
    @NotNull
    @Property(name = "has_usable_output")
    public Boolean hasUsableOutput;

    // 必返，按 MODEL、AGENT 顺序；同一轮一条或两条。
    @NotNull
    @Size(min = 1, max = 2)
    @Property(name = "records")
    public List<ImageVersionSummaryView> records = List.of();
}
```

#### ImagePreviewOutputView

```java
public class ImagePreviewOutputView {
    // 必填。所属候选。
    @NotNull
    @NotBlank
    @Property(name = "candidate_id")
    public String candidateId;

    // 必返，所属逻辑版本。
    @NotNull
    @Min(1)
    @Property(name = "version")
    public Integer version;

    // 必返。
    @NotNull
    @Property(name = "execution_type")
    public ImageExecutionTypeView executionType;

    // 必填。选中预览对应的 ImageVersion.id。
    @NotNull
    @NotBlank
    @Property(name = "record_id")
    public String recordId;

    // 必填。唯一有效不透明成品。
    @NotNull
    @NotBlank
    @Property(name = "file_key")
    public String fileKey;
}
```

#### ImageCandidateSummaryView

```java
public class ImageCandidateSummaryView {
    // 必填。稳定候选 ID。
    @NotNull
    @NotBlank
    @Property(name = "id")
    public String id;

    // 必返，展示顺序。
    @NotNull
    @Min(1)
    @Max(6)
    @Property(name = "index")
    public Integer index;

    // 必返，最高已创建版本摘要。
    @NotNull
    @Property(name = "latest_version")
    public ImageVersionGroupView latestVersion;

    // 可空；无有效不透明成品时为 null。
    @Property(name = "preview_output")
    public ImagePreviewOutputView previewOutput;
}
```

#### ImageCandidateView

```java
public class ImageCandidateView {
    // 必填。稳定候选 ID。
    @NotNull
    @NotBlank
    @Property(name = "id")
    public String id;

    // 必返，展示顺序。
    @NotNull
    @Min(1)
    @Max(6)
    @Property(name = "index")
    public Integer index;

    // 必返，最高已创建版本摘要。
    @NotNull
    @Property(name = "latest_version")
    public ImageVersionGroupView latestVersion;

    // 可空；无有效不透明成品时为 null。
    @Property(name = "preview_output")
    public ImagePreviewOutputView previewOutput;

    // 必返，默认 []；允许多轮同时执行。
    @NotNull
    @Property(name = "active_versions")
    public List<ImageVersionGroupView> activeVersions = List.of();
}
```

#### ImageDesignSummaryView

```java
public class ImageDesignSummaryView {
    // 必填。Design ID。
    @NotNull
    @NotBlank
    @Property(name = "id")
    public String id;

    // 必填。归属商家。
    @NotNull
    @NotBlank
    @Property(name = "merchant_id")
    public String merchantId;

    // 必返。
    @NotNull
    @Property(name = "operation")
    public ImageOperationView operation;

    // 必返。
    @NotNull
    @Property(name = "status")
    public ImageDesignStatusView status;

    // 必返。
    @NotNull
    @Property(name = "has_usable_output")
    public Boolean hasUsableOutput;

    // 必填。创作创建人，不作为独占所有权。
    @NotNull
    @NotBlank
    @Property(name = "created_by")
    public String createdBy;

    // 必返；服务端时间。
    @NotNull
    @Property(name = "created_time")
    public ZonedDateTime createdTime;

    // 必返；服务端时间。
    @NotNull
    @Property(name = "updated_time")
    public ZonedDateTime updatedTime;

    // 必返，按 index 排序；不装载全量历史。
    @NotNull
    @Size(min = 1, max = 6)
    @Property(name = "candidates")
    public List<ImageCandidateSummaryView> candidates = List.of();
}
```

#### ImageDesignView

```java
public class ImageDesignView {
    // 必填。Design ID。
    @NotNull
    @NotBlank
    @Property(name = "id")
    public String id;

    // 必填。归属商家。
    @NotNull
    @NotBlank
    @Property(name = "merchant_id")
    public String merchantId;

    // 必返。
    @NotNull
    @Property(name = "operation")
    public ImageOperationView operation;

    // 必返。
    @NotNull
    @Property(name = "status")
    public ImageDesignStatusView status;

    // 必返。
    @NotNull
    @Property(name = "has_usable_output")
    public Boolean hasUsableOutput;

    // 必填。创作创建人，不作为独占所有权。
    @NotNull
    @NotBlank
    @Property(name = "created_by")
    public String createdBy;

    // 必返；服务端时间。
    @NotNull
    @Property(name = "created_time")
    public ZonedDateTime createdTime;

    // 必返；服务端时间。
    @NotNull
    @Property(name = "updated_time")
    public ZonedDateTime updatedTime;

    // 必返，按 index 排序，含活跃摘要。
    @NotNull
    @Size(min = 1, max = 6)
    @Property(name = "candidates")
    public List<ImageCandidateView> candidates = List.of();
}
```

#### ImageSafeAreaView

```java
public class ImageSafeAreaView {
    // 必返，0–1 的边缘预留比例。
    @NotNull
    @Min(0)
    @Max(1)
    @Property(name = "top")
    public Double top;

    // 必返，0–1 的边缘预留比例。
    @NotNull
    @Min(0)
    @Max(1)
    @Property(name = "right")
    public Double right;

    // 必返，0–1 的边缘预留比例。
    @NotNull
    @Min(0)
    @Max(1)
    @Property(name = "bottom")
    public Double bottom;

    // 必返，0–1 的边缘预留比例。
    @NotNull
    @Min(0)
    @Max(1)
    @Property(name = "left")
    public Double left;
}
```

#### ImagePlacementOptionView

```java
public class ImagePlacementOptionView {
    // 必返。
    @NotNull
    @Property(name = "placement")
    public ImagePlacementView placement;

    // 必填。用途显示名称。
    @NotNull
    @NotBlank
    @Property(name = "name")
    public String name;

    // 必返。
    @NotNull
    @Property(name = "ratio")
    public ImageRatioView ratio;

    // 必返，像素宽度。
    @NotNull
    @Min(1)
    @Property(name = "width")
    public Integer width;

    // 必返，像素高度。
    @NotNull
    @Min(1)
    @Property(name = "height")
    public Integer height;

    // 必返。
    @NotNull
    @Property(name = "safe_area")
    public ImageSafeAreaView safeArea;
}
```

#### ImagePlatformOptionView

```java
public class ImagePlatformOptionView {
    // 必返。
    @NotNull
    @Property(name = "platform")
    public ImagePlatformView platform;

    // 必填。平台显示名称。
    @NotNull
    @NotBlank
    @Property(name = "name")
    public String name;

    // 必返。
    @NotNull
    @Property(name = "default_placement")
    public ImagePlacementView defaultPlacement;

    // 必返，至少一种合法用途。
    @NotNull
    @Size(min = 1)
    @Property(name = "placements")
    public List<ImagePlacementOptionView> placements = List.of();

    // 必返，默认 []；元素必须是非空白规则文本。
    @NotNull
    @Property(name = "content_rules")
    public List<String> contentRules = List.of();
}
```

#### ImageCandidateCountOptionView

```java
public class ImageCandidateCountOptionView {
    // 必返，当前为 1。
    @NotNull
    @Min(1)
    @Property(name = "min")
    public Integer min;

    // 必返，当前为 6。
    @NotNull
    @Min(1)
    @Property(name = "max")
    public Integer max;

    // 必返，当前为 3；Java 用 defaultValue 避开关键字。
    @NotNull
    @Min(1)
    @Property(name = "default")
    public Integer defaultValue;
}
```

#### ImageLimitView

```java
public class ImageLimitView {
    // 必返，当前为 6。
    @NotNull
    @Min(1)
    @Property(name = "content_images")
    public Integer contentImages;

    // 必返，当前为 1。
    @NotNull
    @Min(1)
    @Property(name = "selected_logo")
    public Integer selectedLogo;

    // 必返，当前为 3。
    @NotNull
    @Min(1)
    @Property(name = "style_reference_images")
    public Integer styleReferenceImages;

    // 必返，当前为 5。
    @NotNull
    @Min(1)
    @Property(name = "direct_edit_images")
    public Integer directEditImages;

    // 必返，当前为 5。
    @NotNull
    @Min(1)
    @Property(name = "edit_reference_images")
    public Integer editReferenceImages;

    // 必返，当前为 5。
    @NotNull
    @Min(1)
    @Property(name = "brand_logos")
    public Integer brandLogos;

    // 必返，当前为 5。
    @NotNull
    @Min(1)
    @Property(name = "brand_references")
    public Integer brandReferences;

    // 必返，当前为 6。
    @NotNull
    @Min(1)
    @Property(name = "brand_colors")
    public Integer brandColors;

    // 必返，当前为 5000。
    @NotNull
    @Min(1)
    @Property(name = "prompt_length")
    public Integer promptLength;

    // 必返，当前为 1000。
    @NotNull
    @Min(1)
    @Property(name = "edit_instruction_length")
    public Integer editInstructionLength;

    // 必返，从正式有效配置读取；文档不虚构线上数值。
    @NotNull
    @Min(1)
    @Property(name = "max_upload_bytes")
    public Long maxUploadBytes;
}
```

### 13.4 内部枚举完整定义

与 AJAX 枚举保持相同的 JSON 稳定值，在 BFF 显式映射；不跨层混用 Mongo 枚举注解。

```java
public enum ImagePlatformView {
    @Property(name = "XIAOHONGSHU")
    XIAOHONGSHU,

    @Property(name = "INSTAGRAM")
    INSTAGRAM,

    @Property(name = "TIKTOK")
    TIKTOK,

    @Property(name = "GOOGLE_BUSINESS_PROFILE")
    GOOGLE_BUSINESS_PROFILE,

    @Property(name = "META_ADS")
    META_ADS;
}
```

```java
public enum ImagePlacementView {
    @Property(name = "NOTE")
    NOTE,

    @Property(name = "PORTRAIT")
    PORTRAIT,

    @Property(name = "SQUARE")
    SQUARE,

    @Property(name = "STORY")
    STORY,

    @Property(name = "COVER")
    COVER,

    @Property(name = "UPDATE")
    UPDATE,

    @Property(name = "FEED")
    FEED,

    @Property(name = "LANDSCAPE")
    LANDSCAPE;
}
```

```java
public enum ImageRatioView {
    @Property(name = "1:1")
    RATIO_1_1,

    @Property(name = "9:16")
    RATIO_9_16,

    @Property(name = "4:5")
    RATIO_4_5,

    @Property(name = "16:9")
    RATIO_16_9,

    @Property(name = "3:4")
    RATIO_3_4,

    @Property(name = "4:3")
    RATIO_4_3;
}
```

```java
public enum ImageBrandModeView {
    @Property(name = "PRESET")
    PRESET,

    @Property(name = "REFERENCE")
    REFERENCE,

    @Property(name = "NONE")
    NONE;
}
```

```java
public enum ImagePromptModeView {
    @Property(name = "RAW")
    RAW,

    @Property(name = "OPTIMIZED")
    OPTIMIZED;
}
```

```java
public enum ImageOperationView {
    @Property(name = "GENERATE")
    GENERATE,

    @Property(name = "EDIT")
    EDIT;
}
```

```java
public enum ImageExecutionTypeView {
    @Property(name = "MODEL")
    MODEL,

    @Property(name = "AGENT")
    AGENT;
}
```

```java
public enum ImageExecutionStatusView {
    @Property(name = "QUEUED")
    QUEUED,

    @Property(name = "RUNNING")
    RUNNING,

    @Property(name = "SUCCEEDED")
    SUCCEEDED,

    @Property(name = "PARTIAL")
    PARTIAL,

    @Property(name = "FAILED")
    FAILED;
}
```

```java
public enum ImageDesignStatusView {
    @Property(name = "RUNNING")
    RUNNING,

    @Property(name = "HAS_OUTPUT")
    HAS_OUTPUT,

    @Property(name = "NO_OUTPUT")
    NO_OUTPUT;
}
```

```java
public enum ImageRoleView {
    @Property(name = "CONTENT")
    CONTENT,

    @Property(name = "LOGO")
    LOGO,

    @Property(name = "STYLE_REFERENCE")
    STYLE_REFERENCE,

    @Property(name = "EDIT_SOURCE")
    EDIT_SOURCE,

    @Property(name = "EDIT_REFERENCE")
    EDIT_REFERENCE;
}
```

### 13.5 product-service 按 file_key 解析的依赖接口

在现有 MediaLibraryWebService 中新增以下方法。返回项复用现有 app.product.api.menu.MediaLibraryView，不重定义素材库对象；调用方必须核对每个 key 属于请求 merchant 且为可用图片。该接口仅供内部校验，不增加 AJAX 入口。

```java
@PUT
@Path("/media-library/resolve-by-file-key")
ResolveMediaLibraryByFileKeyResponse resolveByFileKey(ResolveMediaLibraryByFileKeyRequest request);
```

```java
public class ResolveMediaLibraryByFileKeyRequest {
    @NotNull
    @NotBlank
    @Property(name = "merchant_id")
    public String merchantId;

    @NotNull
    @Size(min = 1)
    @Property(name = "file_keys")
    public List<String> fileKeys;
}
```

```java
public class ResolveMediaLibraryByFileKeyResponse {
    @NotNull
    @Property(name = "media_libraries")
    public List<MediaLibraryView> mediaLibraries = List.of();
}
```

### 13.6 注解之外仍需表达的业务校验

- CreateImageDesignRequest 的 operation=EDIT 时 imageFileKeys 条件必填；GENERATE 时不接受该字段。promptMode/brandMode 在 GENERATE 中分别缺省解析 RAW/NONE，不能给所有入口直接初始化成 RAW/NONE 后再误判 EDIT 的参数组合。
- brandStyleId 仅 PRESET 条件必填；REFERENCE 要求风格参考图，NONE 不携带风格 ID 或风格参考图；platform/placement/ratio 的组合按第 11 节处理。条件必填不是全局 @NotNull。
- 请求列表内的 null/空白元素、HEX 格式、图片角色与商家归属需校验；框架 @NotNull 只约束列表自身，@Size 只约束列表长度。不得因此把 String 列表改成多余的包装对象。
- sourceId 必须指向同 Design、同 candidate、较早且有唯一有效不透明成品的记录；MODEL 与 AGENT 共用用户选定来源，原始素材不能反向撤销前轮已确认的修改。
- 首次 V1 的人工重新生成只允许 FAILED，包含超时；复用原记录和输入，只保留最近结果，不新增尝试历史。用户不点击则持续失败，不自动重试。
- 超时即 FAILED；迟到响应不能覆盖失败终态或重新生成后的结果。透明图只查看，下载与改图来源仅接受有效不透明成品。
- 注解能完成的 null/blank/size/range 校验交给框架；服务层只处理注解不能表达的关联、组合、权限及执行状态规则。

本节仅为 Markdown 中的接口/DTO 定义，不生成或修改正式 Java 业务文件，不加入新的 domain、collection 或实施计划。

### 13.7 本轮定义检查

- 提取文档中的 132 个新接口、DTO、View 和枚举定义，在临时目录使用 JDK 25 与正式项目依赖的 Core-NG API 9.3.0 编译通过；product-service 解析接口的两个 DTO 同时引用了现有 MediaLibraryView 及两个枚举进行检查。
- 检查 491 个字段，175 个 String 字段均标注 @NotBlank；枚举、数值、Boolean、List 和对象未误加 @NotBlank。字段均使用 Java camelCase，JSON/Query 映射均显式标注。
- 这只验证本稿 Java 定义的语法、类型引用及注解使用，不代表已完成正式模块编译、WebService 启动注册、Controller 实现或生成链路测试。既有 14 个 JSON 示例仍可解析。
