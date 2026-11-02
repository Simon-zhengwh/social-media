# Social Media：UAT 部署与 AM Console 对接执行交接

关联 Jira：**[FBR-7713 — social-media deploy to uat](https://chancetop.atlassian.net/browse/FBR-7713)**。用户要求后续通过 TWG 把实际修改、测试／验收、部署结果回写该工单，供整体 review，见第 14 节。

> **For Claude:** 如具备相应技能，使用 superpowers:executing-plans 按任务实施。
> **For Codex／其他执行 agent：** 先阅读工作区和目标子仓库的 AGENTS.md、README.md；以本文的用户最终决定为准，重新核实代码后实施，不重复询问已确认的产品选择。

**Goal:** 将现有 social-media 独立应用部署到 AKS 的 UAT 环境、uat namespace，使用 social-media.connexup-uat.net，由 AM 选择商家后跳转，并完成商家隔离、AM 资源读取和 Azure 文件保存。

**Architecture:** 保留 React／FastAPI 独立站。AM Console 负责员工身份与商家授权、商户资料及资源库 API、专属媒体存储的 SAS 签发与确认；social-media 负责现有生成工作流及任务历史。商户特点以 AM merchant-profile 为唯一来源，底层为 ai-marketing 的 restaurant_brand_profiles，不维护 social 独立商户主档。所有平台对接统一通过 AM Console API，不经过 operation-assistant-api；底层仍由 AM 调用现有 file-service，图片字节上传 Azure。

**Tech Stack:** React、TypeScript、Vite、FastAPI、Python 3.12、MongoDB、AM Console／Core-NG Java、Azure Blob SAS、AKS、Envoy Gateway／HTTPRoute、Terraform、SOPS。

- 整理日期：2026-11-02，Asia/Shanghai。
- 状态：本次对话的执行交接；业务代码与部署尚未实施。
- 执行完成条件：执行 agent 必须使用 Codex 内置浏览器完成真实 UAT 页面自助测试与验收，按第 10 节及[逐步冒烟用例](2026-11-02-media-uat-browser-smoke-cases.md)操作、修复失败并交付证据；单元测试、构建或只跑 Playwright 脚本不替代页面验收。
- 文档位置：D:/java/DRI/social-media/docs/plans/2026-11-02-media-uat-am-console-handoff.md。
- 本文记录用户决定、已核实事实和实施建议，三者分开标注。新增接口名称是建议契约，不代表代码中已经存在。
- 最新 DNS 检查（2026-11-02）：social-media.connexup-uat.net 已生效。本机、Google 公共 DNS 和权威 ns1-01.azure-dns.com 均返回 A=20.1.217.183；Google／权威 TTL=300，与 influencer-outreach 的共享 Envoy Gateway 地址一致。磁盘 08-dns.tf 已正确引用 azurerm_public_ip.fbrdev_envoy_gateway_ip.ip_address，环境仓库当前 HEAD=8ad838d（FBR-7713: social-media deploy to uat），工作区干净。此前 NXDOMAIN／磁盘仍指 public_gateway_ip 的检查已被本次结果覆盖。DNS 阻塞已解除，应用部署、HTTPS 和内置浏览器验收仍待完成；本 agent 未执行 plan／apply，实际执行者及 apply 记录待补，不根据解析成功推断执行过程。
- 代码与验收输入更新（2026-11-02）：已 fast-forward 拉取 social-media 至 1461a66299a8ecc0a1f21c3dba5a5c2e80bddf16，新增商户资料展示，调整背景替换输入／最终图识别／已有 run 查询。用户随后明确 merchant_profiles 对应 AM merchant-profile、ai-marketing 的 restaurant_brand_profiles；迁移保留展示功能但改为经 AM API 读取，不沿用本地资料集合／种子导入方案（第 4.5、7.1 节）。已收到四张 1080×1920 JPG 和海报提示词.txt，生成使用用户的小红书提示词，工作台及独立改图只修改几个字；详见冒烟用例第 2 节、SM04–SM07。拉取前后两份本地交接文档哈希一致；本次没有运行应用或页面验收。

---

## 1. 执行前先读：优先级与范围

### 1.1 本文优先于旧 Java 迁移设计

项目已有 [Java 图片 Domain 与接口设计](../analysis/java-image-domain-api-design.md)，描述的是 AM 页面迁移、ai-marketing-integration-service 图片编排及新 Java domain 的旧方向。

用户本次明确说：**原来想迁到 AM Console，现在不迁移；点击页面后直接跳转，暂时没有 Image tools 迁移这个故事。**

因此本次：

- 保留 social-media 现有 Python 后端、React 前端与工作流。
- AM 侧只实现必要的启动入口、授权和平台 API，不把生成业务重写到 Java。
- 不照搬旧文档的三个新 collection、单模型限制、新版本语义、取消通用规范管理等规则。
- 不采用旧文档的 media-library/{merchant_id}/social-media 文件目录。
- 不重新设计视频、营销 Task 集成或完整任务平台。
- 旧文档可作底层接口线索；若与本次决定冲突，以本文为准。不要覆盖或删除旧设计稿。

### 1.2 用户要求的交付范围

涉及三个独立 Git 仓库：

| 仓库 | 本次职责 |
|---|---|
| D:/java/DRI/social-media | 商家隔离、AM API 对接、Azure 存储适配、现有页面调整、容器化、测试和运行说明 |
| D:/java/DRI/fbr-project | AM 启动入口、单商家访问校验、专属媒体上传与后台服务鉴权 |
| D:/java/DRI/fbr-env-project | UAT AKS 资源、DNS、加密配置／Secret、手工发布步骤 |

不要把一个仓库的改动提交到另一个仓库。遵循各仓库局部指令；Java 实施时使用适用的 Core-NG 编码规范。

fbr-project 存在 .codegraph，理解或定位代码必须先使用 codegraph explore 或对应 MCP 工具，再读取未覆盖内容。social-media 和 fbr-env-project 调查时没有仓库级 .codegraph，不创建索引。

---

## 2. 用户已确认的最终决定

| 议题 | 最终决定 |
|---|---|
| 首期使用者 | AM 内部员工；不是商家端产品，也不是无鉴权演示站 |
| 前端形态 | AM 跳转独立 social-media；不继续把完整工作台迁入 AM |
| 商家选择 | 在 AM 先选定 merchant 后跳转，独立站不提供商家切换；换商家回 AM 重新进入 |
| 商家权限 | 沿用 AM 当前可选范围；代码事实是普通 AM 名下商家、管理员全部商家 |
| 数据归属 | 素材关联、品牌风格、任务和版本全部按 merchant 隔离，同商家的有权限员工共用 |
| 商户资料来源 | 用户明确 merchant_profiles 就是 AM merchant-profile；经 AM API 读取 ai-marketing 的 restaurant_brand_profiles，不维护第二份主档或导入 seejoy 种子；平台视觉风格仍由 social 保存并按商家隔离 |
| 验收商家 | 用户最新改为 MA=UMETEA（UME）、MB=choice brooklyn，覆盖此前 SEE JOY 主商家方案；两家已在指定账号的 AM 页面实际打开资料。测试风格卡片归 UME，商户特点读取各自真实 AM profile；用户提供图片作为测试样例，生成提示词使用品牌名改为 UME 的本地副本。用户认为 SEE JOY 较敏感，本轮不再访问或测试其商家资料 |
| 域名 | https://social-media.connexup-uat.net；用户最新要求使用 social-media 前缀，覆盖中途的 media 域名方案 |
| 入口网关 | 用户最终改回参考 outreach：DNS 指向 fbrdev_envoy_gateway_ip，应用使用 Envoy Gateway／HTTPRoute；覆盖中途 notification／Application Gateway 方案 |
| 部署 namespace | social-media API／Web 及配套资源部署到 uat；这是用户后续明确纠正的决定，覆盖此前沿用参考应用 uat-ai 的建议 |
| DNS 发布分工 | 用户说明域名相关权限未授予部署身份；域名 Terraform apply 交给有权限人员执行。agent 准备改动与交接，继续可做工作，待执行结果后验证域名与页面 |
| DNS 时序 | 域名可先行：基线核对后优先单独交接 DNS 改动，由有权限人员先 plan／apply；应用开发并行继续，不等待业务代码全部完成 |
| 验收工具与方式 | 用户明确要求执行 agent 使用 Codex 内置浏览器，在真实页面逐步测试；必须有操作用例和过程证据，单元测试仅作补充 |
| Jira 关联与交付 | 本次任务关联 FBR-7713；执行 agent 使用 TWG 回写实际工作、三仓库改动、提交／PR、真实页面冒烟证据、部署和未完成项，方便用户集中 review |
| 登录 | 复用 AM 登录，不新增一套员工登录 |
| 资源库能力 | 仅浏览和选用 AM Media Library；不在独立站改名、打标签、删除或新增 AM 资源 |
| 临时图片 | 允许临时上传原图、Logo、参考图等，用于任务或品牌配置，不回写 AM；独立审阅后用户明确：先传 social 后端，校验后由后端向 AM 获取 SAS 并上传 Azure |
| 生成结果 | 首期不回写 AM Media Library；在独立站保留任务历史，提供下载 |
| 初次数据 | UAT 从空库开始，不迁移本地品牌、素材、任务或图片 |
| 文件链接 | 沿用平台公开图片直链；页面与数据接口鉴权，拿到文件链接的人可以查看图片 |
| 平台对接入口 | 用户后续纠正为：素材读取和上传地址申请全部通过 AM Console API，不使用 operation-assistant-api |
| 专属文件目录 | 某 merchantId 下单独的 media 前缀，不与其他业务文件混放 |
| 清理功能 | 本次只规划目录；不实现清理功能，不决定保留时长或数据库清理策略 |
| UAT 发布 | 先手工跑通，不在本期新增 Jenkins 流水线 |
| UAT 规模 | 约 1–3 人同时提交生成／改图，保留现有每批最多 6 个候选能力 |
| 发布保障 | 允许维护窗口；发布前停止新任务并等待活跃任务结束 |
| 后台保存 | 用户退出 AM、登录过期，或可恢复任务在服务重启后继续执行时，仍能自动保存；为 social 后台配置专用服务身份 |
| 异常恢复范围 | 保留现有恢复范围：已有 run 的独立改图／背景替换恢复后能保存；其他异常中断人工重试，不承诺所有工作流任意重启无损恢复 |

### 2.1 独立审阅后的补充确认与技术默认

此前“浏览器 SAS 直传”只是未获用户明确回答的默认建议。独立 subagent 审阅后，用户已明确选择：**先传 social 后端，复用现有上传接口，由后端统一校验和保存。** 该选择覆盖早期浏览器直传的默认方案。

**最终链路：浏览器 → social 同源上传 API → 后端校验图片 → AM 签发受控目录的 SAS → social 后端 PUT Azure → 确认文件并保存资产／映射。** SAS 不下发浏览器；本期不增加浏览器直传所需的 staging／final 上传状态机。

后台生成结果也由 social 后端通过 AM 上传。用户无需重新确认独立站、namespace=uat、商家权限、公开文件链接、空库和既有恢复范围。

通用规范原本全局共享；为贯彻“全部按商家隔离”，本方案默认它也按 merchant 保存。公共平台规则、画布预设和代码内默认文案仍可全局只读，不引入公共可编辑品牌模板。

### 2.2 已放弃的方向，勿继续实施

- 使用 AM 内部 mock Image tools 页面承接完整生图功能。
- social 浏览器直接跨域请求 AM AJAX。
- 以 operation-assistant-api 作为最终素材或上传入口。
- social 直接调用 file-service 作为最终跨系统接入方式。
- 使用 media-library/upload 存全部候选和历史版本。
- 为方便接入 SEO 接口，先把本地文件放公网再请求 /seo/file 转存。
- 多 API Pod、多 Uvicorn worker、无维护窗口滚动发布。
- 首期把 SAS 上传地址交给浏览器直传；最终选择为 social 后端接收、校验和上传。
- 本期实现自动清理、所有工作流可恢复队列或完整生产级高可用。

---

## 3. 调查基线与已有工作保护

本地 Git 基线（social-media 已按用户要求更新；另两仓库为初次调查快照，执行前重新核实）：

| 仓库 | 分支 | HEAD |
|---|---|---|
| social-media | main | 1461a66299a8ecc0a1f21c3dba5a5c2e80bddf16（由 e453bd0 fast-forward 更新） |
| fbr-project | master | e16b7c68669dc829842375baf629b7c174333f5f |
| fbr-env-project | main | 1c44e9d74ca8ea2438cc7674d450db3af0434424 |

文档写入前 social-media 工作区干净。其他仓库存在以下**预先已有**改动，不属于本次调查，不能覆盖、删除或夹带提交：

- fbr-project 未跟踪：
  - frontend/merchant-site/src/test/java/app/merchantsite/service/LocationGetServiceAvailableQRCodeTest.java
  - frontend/merchant-site/src/test/java/app/merchantsite/service/LocationQueryServiceAvailableQRCodeTest.java
- fbr-env-project 已修改：
  - azure/infra/env/.terraform.lock.hcl

这些只是时间点快照。执行时重新查看 git status、HEAD 和局部指令，保留用户后续增加的改动。

---

## 4. 已核实的代码事实与入口

本节为调查事实；行号可能随后续提交变化，接手时应通过符号重新定位。

### 4.1 social-media 当前状态

| 代码入口（相对 social-media） | 事实／实施关注点 |
|---|---|
| backend/app/main.py | FastAPI lifespan 创建 Mongo、对象存储并恢复任务；当前 CORS 只允许本地 Vite；目前未实现 AM 登录／商家授权 |
| backend/app/config.py | 本地／COS 存储配置；输入限制实际 20,000,000 bytes；Core AI 默认指向 UAT |
| backend/app/services/object_store.py | ObjectStore 协议含 put/get/exists/delete/presign/local_path 等；put_bytes 返回 None，不能直接把随机企业 file_key 当原逻辑 key 使用 |
| backend/app/services/storage.py | 原图与 prepared PNG 分开保存；prepared 会缩放供模型使用，原始字节需单独保留 |
| backend/app/services/image_ref.py | 当前通过资产记录的 path／prepared_path 构造模型图片输入 |
| backend/app/services/image_host.py、prompt_optimizer.py、agent_image.py | 当前还有自建图床／Core AI blob／COS 的不同 URL 获取分支，UAT 需统一适配新的稳定文件链接 |
| backend/app/routes/assets.py | 当前上传、素材列表、文件访问和删除均为全局范围 |
| backend/app/routes/brands.py、services/brand_rules.py | 当前品牌和通用规范全局共享 |
| backend/app/routes/whole_image.py、poster_edit.py、background_replace.py、task_center.py | 创建、列表、详情、版本、batch、下载、重试、恢复等都要检查商家范围 |
| frontend/src/App.tsx、api.ts、types.ts | 全局加载素材和品牌；统一增加商家上下文、鉴权启动顺序及 API 调用适配 |
| frontend/src/components/AssetLibrary.tsx、AssetPicker.tsx | 原本读取本地资产库，改为 AM 来源的只读查询与选用 |
| frontend/src/components/BrandStudio.tsx、PosterEditStudio.tsx、BackgroundReplaceStudio.tsx | 保留用户已确认的临时上传能力 |

仅在页面传 merchantId 不够。任务、版本、图片、品牌、快照、去重、候选 batch、源版本引用和下载均必须实际隔离。

### 4.2 AM 现有素材与上传接口

[AM 接口定义](D:/java/DRI/fbr-project/frontend/am-console-site-interface/src/main/java/app/amconsolesite/api/MediaLibraryAJAXWebService.java:24)：

| 已存在接口 | 语义 |
|---|---|
| PUT /ajax/media-library/search | 按 merchant_id 查询，支持 filename、tag、type、formats、skip、limit |
| GET /ajax/media-library/get/:mediaLibraryId | 获取商家素材详情，merchant_id 为查询请求字段 |
| POST /ajax/media-library/upload | base64 图片上传；不是本次保存全部任务文件的入口 |
| POST /ajax/media-library/large-file/init-upload | 签发 SAS；行为取决于 category |
| PUT /ajax/media-library/large-file/complete-upload | 确认上传；行为取决于 category |

资源查询响应包含 media_libraries、total；每条有 id、name、url、key、类型、格式、tags、size、thumbnail 等。分页 limit 最大 100。

底层 product-service 的详情查询同时过滤 merchant_id 和素材 _id；不能只凭素材 id 取任意商家数据。AM 查询服务本身没有统一校验当前员工是否有权操作 body 中的 merchant，social 入口必须补上当前用户访问范围校验。

上传 category 的差异：

- MEDIA_LIBRARY：资源库上传流程，大文件初始化当前仅支持视频。
- ARTIFACT：分流至 ArtifactService，可走通用 SAS，包括图片；不写 Media Library 记录。
- ARTIFACT 当前目录固定为 am-console/<merchantId>/draft 或 AM task number；不是目标目录。
- 当前 ARTIFACT complete 只 stat 文件，未完成本次要求的 merchant／路径归属校验，不可原样当作安全的新接口。

关键实现：

- [ArtifactService](D:/java/DRI/fbr-project/frontend/am-console-site/src/main/java/app/amconsolesite/service/ArtifactService.java:58)：SAS 初始化、文件确认和稳定 URL 构造。
- frontend/am-console-site/src/main/java/app/amconsolesite/web/MediaLibraryAJAXWebServiceImpl.java：category 分流。
- frontend/am-console-site-frontend/src/util/azure-upload.ts：现成 Azure PUT 行为。
- frontend/am-console-site/src/main/java/app/amconsolesite/service/CDNService.java：稳定 URL 生成。
- AM UAT 的 app.OSS.URLPrefixOrigin 调查值是 https://fbrdevstorage.blob.core.windows.net/static；不要硬套 operation API 使用的另一 CDN 前缀。

### 4.3 AM 登录与商家权限

已核实：

- cookie 名 AMSessionId，Domain=connexup-uat.net、Path=/、Secure、HttpOnly、SameSite=Lax。
- 30 天是服务端 session 滑动过期，不是 cookie 的 30 天 Max-Age。
- social-media 站点与 AM 同属 UAT 父域 HTTPS 子域，social 后端可以接收 cookie 并转发给 AM 验证，无需前端 JavaScript 读取 cookie。
- 已有 GET /ajax/user/identity 可验证身份。
- 已有 PUT /ajax/merchant/search 支持 keyword／skip／limit／total。
- AM 搜索使用当前 session user id；下游实时判断 Administrator，管理员不过滤 primary AM，普通员工仅名下。
- 当前没有可直接复用的“当前用户可访问指定 merchant”的单商家授权接口。
- merchant profile/get 能返回数据，不等于当前用户获授权。

新增单商家校验可复用：

1. MerchantWebService.get(merchantId)。
2. primaryAMUserId 等于当前 session user id 时放行。
3. 否则调用已注册的 BOUserWebService.checkAdministrator(sessionUserId)；它复用实时 UserRoleService。
4. 非管理员拒绝，商家不存在沿用 404，下游错误不得当作放行。

不要用登录时保存的 session.roles 快照替代实时管理员判断。

入口文件：

- frontend/am-console-site/src/main/java/app/amconsolesite/web/interceptor/AuthorizeInterceptor.java
- frontend/am-console-site/src/main/java/app/amconsolesite/SiteModule.java
- frontend/am-console-site/src/main/java/app/amconsolesite/service/MerchantService.java
- integration/ai-marketing-integration-service/src/main/java/app/aimarketingintegration/service/MerchantService.java
- integration/ai-marketing-integration-service-interface/src/main/java/app/aimarketingintegration/api/BOUserWebService.java

### 4.4 AM 现有 Image tools 只是可复用入口线索

frontend/am-console-site-frontend/src/page/image-tools 已有：

- 真正的 merchant search 和 AM 资源库查询。
- 工作台生成、改图和部分结果仍是 mock。
- merchant selector 当前只取前 100 家，但底层 API 支持分页／搜索。

本次仅复用商家选择流程／菜单位置，改成轻量启动入口，不继续把 mock 工作台接成正式 AM 图片页面。原 /image-tools/:toolId 等可访问 mock 路由应转到启动入口，避免留下两套正式入口；不因本次顺手做大规模前端重构。

### 4.5 商户特点对应现有 AM merchant-profile

用户明确这是同一份业务资料。代码链路已核实：social 同源后端先验证会话及商家权限 → AM GET /ajax/merchant/:merchantId/profile → MerchantService.getProfile → RestaurantBrandProfileWebService.get(merchantId) → ai-marketing RestaurantBrandProfileService.get → Mongo restaurant_brand_profiles。

- [AM 现有读取契约](D:/java/DRI/fbr-project/frontend/am-console-site-interface/src/main/java/app/amconsolesite/api/MerchantAJAXWebService.java:25)；同路径也有 PUT 更新，但 social 本期只读，不调用写接口。
- AM 响应为 merchant_id、brand、locations、artifacts、created_time，没有本地种子的 sections／source_label／source_record_id 包装，需适配现有面板。
- [持久化定义](D:/java/DRI/fbr-project/integration/ai-marketing-integration-service/src/main/java/app/aimarketingintegration/domain/RestaurantBrandProfile.java:21)：实际集合名 restaurant_brand_profiles（复数），@Id merchantId，即 _id 为商家 ID；不另建单数集合。
- AM AuthorizeInterceptor 只验证登录，现有 getProfile 没有 owner／实时管理员校验；仍须先执行第 6.1 节的单商家授权，不能用 profile 返回成功代替授权。
- 资料不存在时下游抛 RESTAURANT_BRAND_PROFILE_NOT_FOUND。只有已获商家授权后的该类缺资料结果映射为空状态；401／403、连接失败、5xx 不得伪装成“没有资料”。

保留左侧商户特点、原始字段来源及空状态。定位／性格／语气取 brand.positioning、brand.personality、brand.voice.description；门店卖点／客群／菜品等按 locations 的真实 place_id 分组，不默认第一家门店代表整个商家。来源标为“AM Console”，展示字段原始值及实际路径，不编造中文摘要，不将完整 artifacts 或与展示无关字段作为“原始资料”透传。

---

## 5. 目标调用链与商家上下文

~~~text
AM（已登录）
  → 搜索／选择有权限的 merchant
  → 打开 https://social-media.connexup-uat.net/?merchant_id=<id>

media 浏览器
  → social 同源 API
      → AM：校验员工会话及 merchant 访问范围
      → AM：查询／获取 Media Library

本地临时图片（用户已确认）
  → social 同源 API 接收文件并校验
  → AM 媒体存储 API 签发 SAS
  → social 后端 PUT Azure 原图／prepared 图片
  → AM 确认文件，social 保存映射并返回可用资产

生成／改图后台任务
  → 从已持久化任务取得 merchant 和资产上下文
  → 使用专用服务身份调用 AM 媒体存储 API
  → 后台 PUT Azure
  → AM 确认文件，social 保存文件映射及任务／版本结果
~~~

实施要求：

- merchant_id 是待授权上下文，不是授权凭证。禁止相信浏览器提供的 userId／operator 作为真实操作人。
- 请求上下文应不可变；不能把“当前 merchant”放在全局变量或跨标签页共享的单个 cookie。
- 推荐新增 social 的统一商家上下文解析，并让业务路由、文件路由和后台服务显式携带该范围；不要为每个页面复制鉴权逻辑。
- 本期商家业务 API 统一采用 /api/merchants/{merchant_id}/...；上下文启动入口为 GET /api/merchants/{merchant_id}/context。原有任务／品牌／资产路径挂在该前缀下，由 api.ts 集中构造，不另保留一套无商家鉴权的兼容业务入口。
- 平台只读预设和基础探活可以继续使用无商家路径，但不得返回用户业务数据或凭证。图片若经 social 路由访问，路径同样带 merchant；公开 Blob／CDN 直链按已确认的公开链接策略处理。
- cookie 鉴权的 POST／PUT／PATCH／DELETE 请求统一校验精确 Origin，UAT 仅信任 https://social-media.connexup-uat.net，并要求由前端 API helper 设置 X-Media-Request: 1。拒绝缺失、null 或其他 Origin 以及缺少该请求头的写请求；包含 multipart 临时上传。SameSite=Lax、同父域和不开放 CORS 不能代替该保护。
- UAT 浏览器只访问同源 API，不继续开放 localhost 开发 Origin。需要本地开发时使用独立明确的开发配置与测试依赖注入，不添加可在 UAT 绕过 AM 鉴权的开关。后台服务凭证接口不使用员工 cookie，也不把浏览器 Origin 当作服务身份。
- 页面先完成上下文验证，再加载品牌、素材、任务；显示已选商家，不提供站内切换。
- 所有页面跳转保留 merchant_id 及现有 view／whole 等有效深链接。
- 缺失商家、未登录、无权限分别显示明确状态与返回 AM 的入口。
- 首期登录过期后可回 AM 登录，再从 AM 重新进入；不额外承诺自动回跳系统。
- 后台作业不持久化员工 cookie，不在日志、错误、模型快照中输出服务凭证或 SAS 凭据。
- AM 响应日志与 Python HTTP 客户端日志需屏蔽 Authorization、服务 key、upload_url 及 SAS 查询参数；不能让 httpx 的请求 URL 日志把带签名地址原样写出。追踪使用 task／merchant／file_key。

---

## 6. AM 新增能力：建议契约与安全边界

下列是可执行的默认技术方案，**尚不存在于当前代码**。可按仓库 API 命名惯例调整名称，不得改变已确认的业务边界。

### 6.1 当前用户访问单商家

建议：

~~~http
GET /ajax/merchant/:merchantId/access
Cookie: AMSessionId=...
~~~

返回当前验证后的 user_id、user_name、merchant_id、merchant_name；不接受调用者指定 userId。执行第 4.3 节的实时权限规则，未登录 401、无权限 403、商家不存在 404。

social 可用一次请求取得身份和授权后的商家，避免每个业务请求分别查询 identity 和全量商家列表。不得把“商家在前 100 条结果中”当作授权判断。

### 6.2 专属媒体存储与后台服务身份

建议新增两项独立能力，不扩展普通资源库入库语义：

| 建议接口 | 请求要点 | 返回要点 |
|---|---|---|
| POST /internal/media/files/init-upload | merchant_id、kind、filename | file_key、upload_url、expires_at |
| PUT /internal/media/files/complete-upload | merchant_id、kind、file_key | file_key、稳定 url、size |

kind 限定 ORIGINALS／PREPARED／OUTPUTS，对应目录 originals／prepared／outputs。

- 由 social 后端调用；服务凭证不下发浏览器。
- 使用独立服务凭证鉴权，只授权这些媒体存储操作，不使它成为 AM 的通用管理员凭证。
- 固定协议为 Authorization: Bearer <service-key>；AM 与 social 的配置项均为 MEDIA_STORAGE_SERVICE_KEY。采用足够随机的独立凭证，通过 SOPS／Secret 注入，比较时使用常量时间方式。凭证缺失／错误一律拒绝，不回退为匿名或通用 cookie 授权，不扩展为新 token 平台。
- 当前 AM 只有 cookie session 拦截，未找到现成可复用的服务身份入口；需要实际补齐鉴权，不能仅加 Anonymous 就开放。
- /internal 只是命名，不代表公网自动不可达。所有请求都必须验证服务凭证；合法调用优先走经核实的服务地址／TLS 路径。
- 当前请求有登录用户时，social 先验证 session 与 merchant 再发起存储；后台续跑使用已授权创建并持久化的任务范围。
- 不新增通用目录上传、任意 URL 转存、任意 Blob 管理或删除能力。
- AM 和 social 分别从环境加密配置／Secret 取得服务凭证，不提交明文。

AM 内部调用现成 FileWebService.initUploadURL／getFileStat 和 CDNService 即可，无需新增 file-service 能力，也无需改 operation API。

### 6.3 目录生成与确认

后端固定：

~~~text
dirs = [merchantId, "media", kind 对应的小写目录]
wrap_uniq_dir = true
~~~

UAT 目标：

~~~text
file_key:
/fbr-uat/product/<merchantId>/media/<kind>/<uuid>/<filename>

物理 Blob:
static/fbr-uat/product/<merchantId>/media/<kind>/<uuid>/<filename>
~~~

其中 static 是 container，不属于 file_key；fbr-uat/product 来自 file-service 的 UAT azure.basePath，不要在应用里把 UAT 前缀写死成所有环境共用。

确认上传必须：

1. 校验服务身份及当前请求的 merchant／kind。
2. 从环境固定 basePath 构造完整、带路径分隔符的预期前缀。
3. 拒绝越界路径、..、反斜杠、伪装 URL／编码路径和不符合签发结构的 key；防止 merchant-a 与 merchant-ab 的前缀碰撞。
4. 验证 key 属于当前 merchant 的 media 目录，不得确认其他业务文件。
5. stat 确认文件存在和实际大小；生成稳定访问 URL。
6. 允许对同一合法对象重复确认，不创建重复 Media Library 记录。

浏览器只向 social 提交文件，不提交企业 file_key 或 SAS。social 根据已验证商家构造安全文件名，取得并保存 AM 返回的 key；确认时不允许把该 key 替换成客户端提供的其他对象。

### 6.4 原图保真与上传协议

Azure PUT 使用 SAS 原始 URL，设置 x-ms-blob-type: BlockBlob 和正确 Content-Type，不额外发送 AM／服务 Authorization 到 Azure。

现有 SAS 为指定单 Blob 的 write/create 权限，HTTPS，有效期调查值为两小时。不要把短期 upload_url 持久化为永久读链接。

这些 SAS 均只在 social 后台使用，不返回浏览器。每次文件写入使用新的不可变对象 key，不覆盖已成为任务／版本输入或输出的原对象。若未来重新设计浏览器直传，必须处理“完成校验后原 SAS 仍可覆盖 Blob”的问题，不能直接把浏览器可写的 key 当作不可变历史原图。

**不要使用普通 POST /file 保存保真海报。** file-service 对 JPG／PNG 默认做 ImageMagick 变换，可能缩到宽 1920、quality 85。SAS 原始字节上传避开该变换。

social 自己的 prepared 预处理与原图保真是两件事：保留原始文件，另存模型用的 prepared 图片；背景替换及要求原始尺寸的流程不得误用缩放后的 prepared 数据。

---

## 7. social-media 数据与资源改造

### 7.1 商家隔离

当前代码新增了本地 merchant_profiles，但用户已明确它对应 AM 现有商户资料。UAT 目标为原七个 social 自有业务集合增加或规范顶层 merchant_id；商户资料改走 AM API，不作为第八个本地主档。brands.merchant_id 使用已授权 AM 商家 ID，不能继续绑定本地种子来源 ID。Mongo／Python 字段名使用 snake_case。Java DTO 如需要对应字段，使用 merchantId 并通过 @Property 映射 merchant_id。

| 集合 | 商家字段 | 约定 |
|---|---|---|
| assets | 必须 | 临时图、AM 输入副本、生成图均属于商家；hash 去重限制在商家内 |
| brands | 必须 | 风格、默认风格、Logo、参考图、封面按商家归属，修改默认风格也仅影响本商家 |
| merchant_profiles（1461a66 的本地实现） | UAT 不使用 | 替换为经 AM profile GET 读取 restaurant_brand_profiles；不在 social 初始化／导入／维护此主档，不改变 ai-marketing 的既有集合结构 |
| brand_rules | 必须 | 每商家一条；_id 使用该 merchant_id，同时保存 merchant_id 字段，替代全局 _id=default；保留版本校验和任务快照 |
| whole_tasks | 必须 | 整图任务、候选 batch、消息和输入快照的归属 |
| whole_versions | 必须 | 从所属 task 继承并校验 merchant_id，保留 task_id，不让浏览器自行指定版本归属 |
| poster_edit_tasks | 必须 | 独立改图、候选 batch 和结果归属 |
| background_replace_tasks | 必须 | 背景替换、run 恢复和结果归属 |
| media_objects（拟新增文件映射集合） | 必须 | 逻辑 key、企业 file_key／URL、孤儿状态均归商家，不是现有集合 |
| app_control（拟新增全局运维集合） | 不需要 | 维护开关／admission 属于整个实例，不是某商家业务数据 |

任务内嵌的 messages、outputs、brand／规则快照等不逐层重复 merchant_id，由外层记录确定归属；其中引用的资产／版本仍要逐一验证同商家。公共平台预设无需转成商家配置。AM 原有 Media Library 已有 merchant_id，本次不是为 fbr-project 所有集合统一加字段，也不迁移成旧 Java 设计的 domain。

商户资料改造：routes/brands.py 的 _brand_detail／_merchant_profile 目前读本地集合，改为从已授权商家的 AM profile 投影，可保留现有 merchant_profile 响应结构适配面板。保存／复制平台风格保留当前 merchant_id，不复制商户主档；同商家不同风格读取同一 AM 资料，无资料显示“商户资料待补充”，不按品牌名称猜测。页面读取使用 AM 真实资料，本期不加持久化资料缓存／同步任务；资料仍仅展示，不自动拼入生图提示词。seejoy.json／import_merchant_profile.py 不纳入 UAT 初始化或迁移，不能以种子填补 AM 缺资料，不删除用户本地历史数据。“UAT 空库”仅指 social 自有新库，不清空或重建 ai-marketing 资料。

版本虽可通过 task 反查商家，本期仍显式冗余 merchant_id，以便统一授权查询和索引；写入值必须来自已授权父任务，保持一致。其他业务记录从已验证请求／后台任务上下文写入，不直接信任 body 中的商家字段。

所有 task center、状态查询、重试、结果刷新、选择版本／输出、下载、模型输入预览都使用上述范围。常规按 ID 的业务查询也采用 {_id: id, merchant_id: authorizedMerchantId}。根据实际查询建立 merchant_id＋排序／关联字段的复合索引；启动恢复等特权全局扫描另建需要的状态索引，不能盲目给所有索引都加商家前缀。

Mongo 无需关系型 ALTER TABLE，但创建／更新、读取、关联校验和索引都要同步改。UAT 空库按新结构开始；不推测本地历史数据归属，不把缺 merchant_id 的旧数据当作所有商家可见。

所有 ID 引用必须验证同商家：品牌、素材、source task／version、batch 中候选、结果 asset。不能只过滤列表却让猜到 id 的详情或下载跨商家访问。

后台生成和重启恢复从任务记录获取 merchant，再创建对应存储／查询上下文。全局 app.state 不得被某次请求切换成某个商家。

### 7.2 素材来源与临时图

- 资源库页和选材器改为 AM 的分页、搜索、标签查询；不再枚举本地全部资产作为 AM 库。
- 客户端提交 media_library_id，后端使用已授权的 merchant 获取真实详情，不接受任意远程 URL 作为 AM 素材。
- 新选材导入 social 任务上下文时保存来源元信息，并将需要保留的原图／prepared 副本保存到自己的 merchant media 前缀，供后续编辑、恢复及历史使用。
- 不把 AM 源对象标记为 social 自有文件，不通过 social 删除 AM 源 Blob。
- AM 的 SVG 不在现有 Pillow 支持范围，不能未经处理直接送现有读取链路。首期按当前图像解码能力筛选格式，不为此额外引入 SVG 渲染平台；对不支持格式给明确说明。
- 临时图不会加入 AM Media Library，也不应被 AM 来源列表误当成资源库条目。
- 临时上传在标记资产可用前校验实际大小、格式、像素、透明度等；保留现有安全边界，不能只信文件扩展名或浏览器 MIME。
- 空库无品牌时提供合理空状态和新建品牌入口，允许使用现有“无品牌”模式。

### 7.3 持久化存储适配

保留 ObjectStore 的最小适配层，持久化：

~~~text
merchant_id + social 逻辑 key
  → 企业 file_key + 稳定 url + 必要的 size/hash/type 信息
~~~

要求：

- 映射不能只存在内存或 Pod 本地缓存。
- 同一逻辑 key 的读取、存在检查、预览、模型引用和清理不得错指其他商家。
- 输出不可变；使用独立对象避免覆盖旧版本或 CDN 缓存混淆。
- 上传／确认失败时不得把“本地暂存成功”当作远端持久化成功。
- UAT 不再依赖自建图床或 COS 才能让模型读取图片；统一使用新平台稳定 URL。
- Pod 本地只作可重建缓存／临时文件，不作为业务图片唯一副本。
- 删除适配不得顺带新增产品清理能力。现有失败路径的临时文件处理需保持明确的所有权；禁止用旧删除逻辑误删 AM 原文件。
- 首期新 AM 存储接口仍只有 init／complete，不增加物理删除接口。将现有生成失败／版本提交回滚中的 ObjectStore.delete 适配为解除自有对象的业务可见性，并持久化 orphan 标记、file_key、merchant 和时间供未来清理；保留现有 best-effort 失败处理，不让清理错误覆盖原始生成错误。
- 孤儿对象不进入素材列表或成功结果，映射／追踪记录不能随资产记录一起丢失，后续写入也不能覆盖掉旧孤儿 key。已上传但尚未确认的自有 key 也应留下可追踪记录。不允许把 delete 变成无记录的静默 no-op，也不把本次扩成自动清理产品。

---

## 8. AKS、域名与发布事实

### 8.1 参考应用与本次部署位置

**用户最终确认：social-media 发布到 uat namespace，入口参考 influencer-outreach 的 Envoy Gateway／HTTPRoute，DNS 引用 fbrdev_envoy_gateway_ip；不继承参考应用的 uat-ai namespace。中途选择 notification／Application Gateway 的决定已被覆盖。**

| 对象 | 环境 | Kubernetes namespace |
|---|---|---|
| social-media API／Web 及其 ConfigMap、Secret、Service、HTTPRoute | UAT | uat |
| 参考应用 influencer-outreach | UAT | uat-ai |
| 现有 AM Console（包括本次新增的 AM 接口） | UAT | uat |
| 复用的 Mongo、file-service 等现有服务 | UAT | uat（保持现有部署位置） |

配置路径 azure/uat/... 中的 uat 表示环境目录；实际资源部署到哪个 namespace，以 YAML 的 metadata.namespace 为准。social 资源统一使用 uat，AM 改动仍更新原来的 uat 应用。envpool=aiapp 是参考应用的节点调度标签，不等于 namespace；不要因复制参考配置而把 social 误部署到 uat-ai，也不要把修改 namespace 理解为必须同时修改节点池。

[47-influencer-outreach.yml](D:/java/DRI/fbr-env-project/azure/uat/kube/resource/app/ai/47-influencer-outreach.yml:1)：

- namespace=uat-ai，api／web 两个 Deployment。
- ACR 前缀 fbrdevacr.azurecr.io/uat/ai/。
- 调度节点标签 envpool=aiapp。
- 入口使用 Envoy Gateway 的 HTTPRoute，不是旧 Nginx Ingress。
- 同域路径分流：参考应用是 /api/v1；social 应按自己的 /api 适配。
- 参考应用的长超时、PVC、探活路径、RollingUpdate 参数不可机械照抄。

本次沿用 influencer-outreach 的入口方式，HTTPRoute 的 parentRefs 指向 envoy-gateway-system/internal-gateway、sectionName=https，调整 namespace、hostname 和后端服务。notification 与 AM 自身仍使用 Application Gateway Ingress，不因此改变；social 的 DNS 与路由必须对应 Envoy。

配置与域名入口参考：

- azure/uat/kube/resource/app/ai/config/47-influencer-outreach-config.yml
- azure/uat/kube/resource/app/ai/secrets/enc/47-influencer-outreach-secret.enc.yml
- azure/infra/env/08-dns.tf：现有 connexup-uat.net zone；social_media_uat 目标引用 azurerm_public_ip.fbrdev_envoy_gateway_ip.ip_address，与 outreach 一致；最终交接前核对磁盘保存内容。
- azure/infra/helm/gatewayapi/gateway-internal.yml：现有 Envoy Gateway 的 HTTPS listener 与跨 namespace 路由许可；referencegrant-tls.yml 配置证书引用许可。
- azure/infra/helm/certmanager/cert/uat-certificate.yaml：现有 *.connexup-uat.net 通配证书。

本次复用现有 Envoy Gateway，不新建网关。其 https listener 允许所有 namespace 的路由，TLS 引用 uat 中已有通配证书。internal-gateway 的名字不代表仅内网访问，仍须实施登录和商家授权。部署时核对 HTTPRoute 的 Accepted／ResolvedRefs 状态及证书引用许可；不要改动共享 Gateway 或为 social 配置 Application Gateway Ingress。

### 8.2 social UAT 目标

- 部署位置：UAT 环境，namespace=uat；social 的 Deployment、Service、ConfigMap、Secret、HTTPRoute 等资源均设置 metadata.namespace=uat，部署／查询命令显式指定 -n uat。
- 域名：social-media.connexup-uat.net；A 记录引用 fbrdev_envoy_gateway_ip。AM 跳转、HTTPRoute hostnames、公开 base URL 和 Origin 均使用此域名。
- 入口使用 gateway.networking.k8s.io/v1 HTTPRoute，parentRefs 指向 envoy-gateway-system/internal-gateway、sectionName=https；/api 的 PathPrefix 路由至 API Service，其余 / 路由至 Web Service，不去掉 FastAPI 已有 /api 前缀。
- 复用共享 Gateway 的 HTTPS listener 及其引用的 uat/fbr-uat-net-tls 通配证书，不另购域名或新建网关／重复证书；HTTP 到 HTTPS 的重定向按现有 Gateway 能力单独核实并验收。
- API／Web 分别使用实际可用的 HTTP 探活路径。按同步提示词优化的真实耗时设置 HTTPRoute request／backendRequest 超时，必要时参考现有 Envoy BackendTrafficPolicy；不能机械照抄 outreach 的一小时超时。后台生成仍以 202＋轮询执行，不依赖保持 45 分钟的浏览器请求。
- 前端生产构建为静态站，不能把 Vite dev server 当正式 Web 容器。
- 后端 Python 3.12、Uvicorn 单 worker；前端 Node 22 构建，分别提供 Dockerfile。
- 镜像采用可追溯 commit tag，不只依赖 latest；交付手工 ACR 构建、首次初始化与更新步骤。
- 使用共享 UAT Mongo 的独立应用数据库；上线前检查拟用库名，不覆盖同名已有数据。
- 非敏感配置用 ConfigMap，凭证按现有 SOPS／Secret 方式管理。
- AM 和 social 均在 uat；服务凭证按现有 Secret 规范注入两个应用，可引用同 namespace 的受控凭证 Secret，无需为跨 namespace 额外复制凭证。
- 真实业务文件已在 Azure，不为本地图片唯一存储机械复制 outreach PVC；临时目录使用受限可写卷。
- 为只读根文件系统、非 root、tmp 容量、图片解码内存和日志输出配置合理资源。
- 按 1–3 人、最多 6 候选的代表性负载验证容量；不能把试用人数等同于后台只有三个模型调用。
- 先手工发布并给出可重复执行的 runbook，本期不新增 Jenkins 作业。

AM Service 当前仅暴露 8080，不是内部 443。Core session 需要 HTTPS scheme，裸 HTTP 内网调用不能直接假定 cookie 验证可用。优先使用经核实的 HTTPS AM 地址；若使用集群内 HTTP，必须由可信 BFF 固定设置正确的代理 scheme，并验证现有 framework 行为，不转发浏览器任意代理头。

### 8.3 Mongo 初始化

UAT Mongo 配置为 7.0.30、replicaSet=fbr-uat，开启 notablescan=1。本地 compose 是 Mongo 8，不能把本地通过等同于 UAT 查询通过。

按实际查询补齐索引，重点包括：

- 按 merchant 的任务／品牌／资产列表与排序。
- 版本的 task_id／version 查询。
- merchant 内 hash 去重和外部素材引用。
- batch／candidate 查询。
- 启动恢复的 status、edit.status、run_id 查询。
- 仍保留的引用检查涉及的 $or 查询。

索引初始化必须先于恢复查询；初始化失败 readiness 应失败，不能 ping 成功就宣告应用完全就绪。liveness 不应因短暂 Mongo／模型不可用不断重启应用。

#### 8.3.1 首发要初始化什么，UME 数据如何准备

用户询问是否需要初始化数据。首期采用以下最小方案，不改变“social 从空库开始、不迁移本地历史”的决定：

| 项目 | 首发处理 |
|---|---|
| social 应用库／集合／索引 | 核对独立库名和读写／建索引权限，按实际查询创建集合及索引；不靠插入假任务来建集合。Mongo 的集合可由建索引或首次正常写入创建 |
| 全局维护控制 | 启动时幂等创建 app_control 的 _id=maintenance；仅缺失时初始化 enabled=false、admitting=0。已有维护状态不被初始化覆盖；崩溃遗留 admitting 仅按第 8.4 节单进程恢复约定处理 |
| 商家通用规范 | 无该 merchant 记录时读取代码 DEFAULT_RULES（当前默认 version=1），不必预灌数据库；首次通过已授权、已校验 CSRF 的正常保存创建 _id=merchant_id／merchant_id 记录并执行版本校验。已有规则不得被启动默认值重置，不把全局 _id=default 导入给所有商家 |
| 平台／画幅／模型配置 | 平台和尺寸预设复用代码；Agent ID／模型及非敏感环境项走 ConfigMap、凭证走 Secret。这些不是商家资料种子 |
| UMETEA／choice brooklyn 商户资料及 AM 资源库 | 经 AM 查询并记录真实商家 ID，复用各自现有 merchant-profile 和 Media Library；social 不创建／导入第二份 merchant_profiles，不运行本地 seejoy.json 导入，不通过本次初始化写入 AM 主档或资源库 |
| UME 平台视觉风格 | 首轮按 SM04 在内置浏览器新建 UME_SMOKE_<RUN_ID>_A 小红书风格、关联临时 Logo／参考图，并正常保存商家规范；按用户要求将样例风格视作 UME 的测试风格。用这一步同时准备数据和验证写入；不修改 AM 真实资料或自动导入本地品牌 |
| 任务／版本／资产／文件映射 | 由实际页面上传、生成和修改操作逐步产生；初始无历史记录正常，不插入伪造 succeeded 任务。已有 Azure Blob 容器内的 media 路径由首次正常文件上传形成前缀，不必预先创建空目录 |

首次验收前先记录“空 social 库＋现有 AM 资料”的基线，再执行 SM01–SM04；SM04 创建的风格用于后续生成，AM 素材读取由 SM02 验证。重复初始化／重启应保持维护状态、规则版本、风格、任务和图片映射不变；补充自动化检查幂等性，页面中以 SM13／SM14 验证实际持久化与维护行为。

以上是待实施的初始化要求。当前 lifespan 主要做 Mongo ping、中断任务处理和少量索引，不能据此声称新初始化能力已完成；执行时先补齐索引先于恢复、维护记录及 readiness 的实现，再记录真实初始化结果。本轮只更新方案，未连接 UAT 写数据。

#### 8.3.2 初始化在哪里执行，是否需要本机直连 UAT Mongo

首期由部署到 AKS uat 的 social API 在启动阶段执行基础初始化，程序通过集群内 Mongo 地址连接自己的应用库。本机负责代码和部署操作；不要求本机能直连 Mongo，也不要求用户提供 Mongo 公网入口。

执行顺序：

1. 在 fbr-env-project 配置经过核实的 Mongo 集群内拓扑／应用库名，凭证由 Secret 注入；URI 若含凭证也放 Secret，不写进明文 ConfigMap。环境仓库存配置及资源定义，不存业务数据。
2. social API Pod 启动后，在自己的数据库创建／核对索引，幂等补齐维护控制记录，然后执行第 8.4 节的中断／恢复处理，全部完成才 readiness 成功。
3. 索引及控制记录确实会写入 UAT Mongo，但由随版本发布的应用代码执行，不通过人工登录数据库随手改记录。重复启动复用相同索引，初始化控制记录用缺失时插入的方式，已有维护状态和业务数据保持原值。
4. UME 风格／规范／临时图／任务由内置浏览器操作正常业务 API，运行中的后端按已授权 merchant_id 写入 Mongo；AM merchant-profile 和素材库继续经 AM API 只读获取。空库首发无需给旧记录批量补 merchant_id。

执行需要区分网络与权限：AKS Pod 必须能连到目标 Mongo；应用数据库身份需要自身库的正常读写与建索引权限。应用能正常保存数据，不代表一定有建索引权限，实施时先核实。若缺数据库权限，由负责 Mongo 的人员为该应用库配置所需权限或按审阅后的索引定义执行初始化；不通过开放公网 Mongo 解决。执行 agent 还需核实自身对 AKS 部署／日志查询的权限，不能仅因本机装有 kubectl 就宣称能完成发布。

Mongo 初始化随应用启动生效；Terraform 的 DNS apply、Git 提交或仅修改环境文件不会替它写数据库。初始化结果以应用日志／readiness、必要的只读数据库检查及真实页面保存回读为证据。本节只明确后续执行方式，尚未实际验证集群连通／数据库身份权限或执行 UAT 初始化。

### 8.4 长任务与维护发布

当前行为：

| 工作流 | 现有重启恢复 |
|---|---|
| 整图生成 | queued／running 标失败，需人工重试 |
| 整图工作台内的改图 | running 编辑标失败；Agent run_id 未及时落库，不能按现有逻辑恢复 |
| 独立“修改海报” | 已保存 run_id 和输入时继续查询原 run，不重新触发 |
| 背景替换 | 已保存 run_id 时继续查询，并保留原 deadline；可手动查询已有结果 |

main lifespan 的全库 mark_interrupted／resume 没有多进程 owner／lease。多 worker 或新旧 Pod 重叠会误标旧任务、重复轮询和保存。

因此首期：

1. 单 API Pod、单 worker，不开启 HPA。
2. 采用 Recreate 等不重叠更新策略。
3. 提供受控维护开关和活跃任务检查：暂停新的写请求，覆盖生成、改图、重试／恢复触发、付费提示词优化、上传／素材导入及品牌等修改；允许查看已有状态与下载，已开始的后台执行可以继续保存。
4. 等正在执行的任务结束再更新；不能拿 30 秒 termination grace 当 45 分钟任务保障。
5. 确认初始化、存储映射及允许恢复的任务正常，再恢复新任务提交。
6. 不自动重新触发远端模型；保存已有 run 的结果不应因为员工退出 AM 而失败。
7. 本期不承诺全部模型请求在任意进程故障下 exactly-once／自动恢复。

维护的默认实现固定为共享 Mongo 状态和后台 CLI，不提供面向普通员工的维护管理页面或公开切换接口：

- 使用单例控制记录，例如 app_control 中 _id=maintenance，保存 enabled 与 admitting 计数。初始化控制记录先于接单，重启不得自动清除 enabled。
- 接受新写操作前，用 enabled=false 条件原子取得 admission 并递增 admitting；该操作的任务记录／状态持久化后才释放计数，异常路径也释放。同步提示词优化等没有任务记录的长请求，整个执行期间保持 admission。
- CLI enable 原子设置 enabled=true；与 admission 使用同一控制记录，避免先检查开关、稍后插入任务的竞争。CLI status／wait 同时检查 admitting=0、全部 queued/running 任务和正在执行的 edit，不只看某一个任务 collection。
- CLI 命令约定为 python -m scripts.maintenance enable、status、wait、disable；使用执行环境的 Mongo 配置。门禁关闭返回明确 503，前端禁用相应操作；CLI 只在排空后报告可更新。
- 单 API 进程在确认没有旧进程重叠的启动阶段，重置崩溃遗留的 admission 计数并执行既有任务中断／恢复，完成初始化后再 readiness 成功。Recreate／单 worker 是该简化实现的前提，禁止据此开启多进程。

---

### 8.5 fbr-env-project 的具体文件与内容

fbr-env-project 保存“UAT 如何运行这些应用”的环境配置。应用源码与 Dockerfile 留在 social-media；AM 接口实现留在 fbr-project。下面是建议的文件组织，目前尚未创建；执行前重新确认序号和文件名未被其他工作占用。

~~~text
fbr-env-project/
├─ azure/uat/kube/resource/app/56-social-media.yml
├─ azure/uat/kube/resource/app/secrets/enc/56-social-media-secret.enc.yml
├─ azure/uat/kube/resource/app/54-am-console-site.yml          （修改现有文件）
├─ azure/infra/env/08-dns.tf                                 （增加 social-media 记录）
└─ azure/uat/kube/social-media-deployment.md                  （手工发布说明）
~~~

| 文件／配置 | 保存的信息 |
|---|---|
| 56-social-media.yml | namespace=uat 的 API／Web Deployment、Service、ConfigMap、Envoy HTTPRoute 及必要的路由策略；镜像与 tag、端口、资源限制、探活、临时卷、单副本／单 worker 及不重叠更新策略 |
| 同文件中的 ConfigMap | UAT 公共域名、AM API 地址、无凭证的 Mongo 连接拓扑／库名、Core AI UAT 地址／Agent ID／模型等非敏感配置；变量名按最终代码配置接口对应 |
| 56-social-media-secret.enc.yml | SOPS 加密的 Core AI API key、AM 媒体存储专用服务凭证、Mongo 认证信息（如有）；通过 Secret 注入，不能把明文放进 ConfigMap 或提交 backend/.env |
| 54-am-console-site.yml | 为 AM 引用同 namespace 的媒体服务凭证及必要环境配置；保持 AM 原 Deployment／Service 的位置和现有功能 |
| 08-dns.tf | social-media.connexup-uat.net 的 A 记录，引用现有 UAT DNS zone 与 azurerm_public_ip.fbrdev_envoy_gateway_ip.ip_address，不硬编码解析结果 |
| social-media-deployment.md | 手工构建／推送镜像、加密配置的使用、首次资源初始化、维护发布、状态检查、冒烟验证及回滚步骤；只记录操作与配置项名，不记录密钥值 |

可在加密文件中组织 social 私有 Secret 与 AM／social 共用的媒体服务凭证 Secret，按最小需要给 Deployment 引用。AM 与 social 都在 uat，无需为了本次应用另建 namespace。

复用现有 Azure Blob、共享 Mongo、Envoy Gateway 与通配证书。本次只补应用运行配置和 DNS，不重新创建这些基础设施。具体 merchantId、品牌、任务、文件映射与图片链接属于运行数据，不逐项写入环境仓库。

原始图片和生成图片保存在 Azure，任务／版本／映射保存在 Mongo；镜像保存在 ACR。fbr-env-project 的 Git 保存资源定义和配置，不存这些运行产物。首期手工发布，暂不新增 Jenkins 流水线。

### 8.6 修改环境仓库后，如何真正应用到 UAT

**本期采用手工发布：修改文件、提交／合并 Git 不代表配置已经在 Azure／AKS 生效。agent 执行权限范围内的应用部署和检查；用户已指定域名相关 Terraform apply 交给有权限人员执行，agent 不自行执行该部分。** 纯文档变更不需要部署；运行资源按下表分别处理。

| 改动 | 生效操作 |
|---|---|
| Deployment／Service／ConfigMap／HTTPRoute YAML | 对本次新增／修改的具体文件执行 kubectl apply，namespace=uat；先做服务端 dry-run／必要的非敏感 diff |
| SOPS 加密 Secret | 按仓库单文件解密流程生成受控的明文输入，确认解密成功后 apply；不把 enc.yml 的加密值直接作为运行 Secret 应用，不输出明文凭证 |
| 08-dns.tf 中 social-media A 记录 | agent 优先单独准备配置／diff／交接；有权限人员在现有 azure/infra/env 根模块和既有远程 state 下 plan、核对并 apply，agent 收到执行结果后验证 |
| 应用代码／Dockerfile | 先构建并推送 ACR 的实际新镜像，再更新 Deployment 镜像 tag；单独 apply 环境 YAML 不会编译或发布新的业务代码 |

现有 UAT Jenkins 模板的 set-image 阶段主要使用 kubectl set image（例如 azure/uat/build/job-templates/frontend.groovy），不能据此假定新增 Secret／Service／HTTPRoute 和 Terraform DNS 会自动应用。本期不新增 Jenkins 作业，按手工 runbook 执行。

**Kubernetes 操作模板**（工作目录 D:/java/DRI/fbr-env-project；文件／资源名是第 8.5 节的拟定名称，尚未创建。执行前核实当前 context 对应目标 AKS，写操作使用已核实 context）：

~~~powershell
kubectl config current-context
kubectl -n uat apply --dry-run=server -f azure/uat/kube/resource/app/56-social-media.yml
kubectl -n uat diff -f azure/uat/kube/resource/app/56-social-media.yml
# 先按单文件流程解密并应用所需 Secret，再应用引用它的资源。
kubectl -n uat apply -f azure/uat/kube/resource/app/56-social-media.yml
# AM Deployment 有变更时，单独应用该文件并核对实际 AM 镜像。
kubectl -n uat apply -f azure/uat/kube/resource/app/54-am-console-site.yml
kubectl -n uat rollout status deployment/social-media-api
kubectl -n uat rollout status deployment/social-media-web
kubectl -n uat get pods,services,httproutes
~~~

首次创建之前没有本应用活跃任务；后续更新必须按第 8.4 节先启维护、排空。镜像／Pod template 变化会触发更新；仅修改 envFrom 引用的 ConfigMap／Secret 内容，不会刷新运行进程的环境变量，需在排空后对受影响 Deployment 执行受控重建／rollout restart，再核对 readiness。不能对仍有活跃任务的 social API 直接重启。AM 的既有发布顺序仍遵守先提供兼容接口、再开放 social 入口。

**Terraform DNS 操作模板——供有权限的 DNS 发布执行者使用，不是要求 agent 自行 apply**（同一工作目录）：

~~~powershell
# 以下由有权限人员对交接的实际提交执行。
terraform -chdir=azure/infra/env init
terraform -chdir=azure/infra/env validate
$mediaPlanPath = Join-Path $env:TEMP 'FBR-7713-media-uat.tfplan'
terraform -chdir=azure/infra/env plan "-out=$mediaPlanPath"
terraform -chdir=azure/infra/env show -no-color $mediaPlanPath
# 实际核对计划后，再应用该计划，不是重新生成另一份未审阅的计划。
terraform -chdir=azure/infra/env apply $mediaPlanPath
~~~

该根模块还管理共享 AKS、网络、证书等资源。先核实 Azure subscription、既有 backend／workspace 和仓库要求的 Terraform 版本；不创建新空 state、不随意升级 provider，不把用户已有 lockfile 变更夹带进去。计划应只含本次预期改动；若出现无关变更或共享资源重建／删除，先查明并解决，不直接整体应用。计划文件可能带敏感信息，留在临时目录，不提交或上传到 Jira。

**DNS 交接与等待约定：**

1. agent 优先准备 08-dns.tf 的 social-media A 记录并交接，提供实际 diff／后续获准提交时的 commit／PR、Terraform 目录与 backend／workspace 说明、zone=connexup-uat.net、记录名 social-media、目标 azurerm_public_ip.fbrdev_envoy_gateway_ip.ip_address 及验证说明。该 IP 已存在，DNS plan／apply 不依赖应用代码、容器或 HTTPRoute 已完成，可先行执行，开发并行继续；保留用户当前先看改动、未经后续指示不自动提交的要求。
2. agent 在已有权限内做格式／静态检查；若读取远端 state 或生成 plan 也缺权限，明确“plan 待有权限人员执行”，不伪造已审阅计划，不因 DNS 阻塞停掉全部开发／测试。
3. 有权限人员对同一实际提交自行 plan、核对预期变化后 apply，并反馈执行提交、结果、时间与必要的资源信息。不要通过聊天／Jira 传递含敏感信息的二进制 plan、state 或凭证。agent 不擅自找人发消息或更改负责人，由用户安排执行者。
4. 等待期间继续代码、离线／集成测试、容器构建及权限范围内的 Kubernetes 部署。DNS 尚未 apply 时标记“等待 DNS apply”；DNS 已完成但应用／HTTPRoute 未就绪时标记“DNS 已完成，等待应用部署”，不能混为同一阻塞。
5. 有权限人员完成后，agent 先核对 social-media.connexup-uat.net 解析至共享 Envoy Gateway 公网 IP。HTTPRoute 尚未部署时可能没有业务页面，不代表 DNS apply 失败。等应用／HTTPRoute 就绪，再验证完整 HTTPS 和内置浏览器 SM01–SM15。临时域名、hosts、直连 IP 或端口转发只作联调辅助。

应用后检查实际镜像／Pod、HTTPRoute parentRefs／hostnames／路径规则／Accepted／ResolvedRefs、共享 Gateway HTTPS listener 与证书、后端 Service 及依赖连通；DNS apply 交接完成后再验证域名及页面。最后将实际发布、DNS 执行分工与结果、验收及等待项经 TWG 回写 FBR-7713，不能以 apply 返回成功替代页面验收。

官方行为依据：[Terraform plan](https://developer.hashicorp.com/terraform/cli/commands/plan) 仅产生计划，[Terraform apply](https://developer.hashicorp.com/terraform/cli/commands/apply) 执行计划；[Kubernetes ConfigMap](https://kubernetes.io/docs/concepts/configuration/configmap/) 说明环境变量方式的配置更新需要 Pod 重启。本节只是后续操作说明，本次文档整理没有执行云端 apply。

## 9. 建议执行任务顺序

每项按“先补有意义的行为／权限测试 → 最小实现 → 受影响验证”推进。当前没有实现以下任务，不得把清单当已完成记录。

总时序：基线核对 → DNS 独立改动与权限人员交接 → DNS plan／apply 与应用开发并行 → AM／social 应用部署 → 正式域名页面验收。无需等全部业务代码写完才申请域名。

### Task 1：重新建立基线

- [ ] 阅读三仓库适用 AGENTS.md／README，查看 HEAD、git status。
- [ ] 使用 CodeGraph 核实 AM／file-service 契约和权限规则。
- [ ] 保护第 3 节已有改动，确认空 UAT 数据与现有部署资源。
- [ ] 保留本方案与旧 Java 迁移稿的优先级说明。
- [ ] 确认 social 的目标 namespace 为 uat；参考应用位于 uat-ai 不构成沿用其 namespace 的理由。

### Task 1A：DNS 先行，交接后并行开发

- [x] 已核对磁盘 social_media_uat 引用 fbrdev_envoy_gateway_ip、TTL=300；环境仓库当前 HEAD=8ad838d，工作区干净，不重复新增／提交 DNS。此前 fmt／validate 为历史检查，本次未重跑；后续修改 Terraform 时重新执行。
- [x] DNS 已实际验证：本机、Google 公共 DNS、Azure 权威 DNS 均解析 social-media.connexup-uat.net 至 20.1.217.183，权威 TTL=300；原“等待 DNS apply”阻塞解除。
- [ ] 补充实际 DNS 执行者／apply 结果记录；agent 不自行补跑 apply。继续 Task 2–7，应用／HTTPRoute 就绪后验证 HTTPS 和页面，不能把 DNS 成功当成应用已上线。

### Task 2：AM 单商家授权

- [ ] 为 owner、实时管理员、其他员工、无会话、不存在商家、下游错误增加测试。
- [ ] 在 AM interface／site 增加当前 session 的单商家访问上下文。
- [ ] 复用已有 MerchantWebService 和 BOUserWebService，不增加新的 integration 授权平台。
- [ ] 调用方身份必须来自 session。

### Task 3：AM 媒体存储接口

- [ ] 先测试服务凭证、固定目录、跨商家／跨业务 key 拒绝、stat 不存在和重复确认。
- [ ] 实现 SAS init／complete，底层复用 file-service 与 AM URL builder。
- [ ] 接入单独服务身份；在测试、开发配置与 UAT Secret 中正确注入，不使用真实凭证作夹具。
- [ ] 不变更普通 Media Library／ARTIFACT 的既有目录和入库行为。

### Task 4：social 商家上下文与数据隔离

- [ ] 集中实现 AM 登录／商家验证，启动页面先加载上下文。
- [ ] 所有相关记录带 merchant_id，所有资源 ID／版本来源按商家校验。
- [ ] 将本地 merchant_profiles 读取替换为 AM 现有 profile GET；先验会话及商家权限，适配字段投影／原始来源／多门店／缺资料与失败状态。保存及复制风格保留当前 merchant_id，不维护第二份主档、不导入本地种子、不自动注入生图提示词。
- [ ] 固定商家路径前缀，集中实现 Origin／X-Media-Request 防护，覆盖 multipart 写请求；没有浏览器防护的后台入口必须另行通过服务凭证验证。
- [ ] 创建索引与初始化 readiness；空 UAT 不执行本地历史导入。
- [ ] 按第 8.3.1 节实现幂等基础初始化：控制记录只在缺失时创建，已有规则／维护状态不重置；通用规范缺失时返回代码默认，首次正常保存按商家创建。UME 测试风格／资产／任务由真实页面准备，不自动灌种子。
- [ ] 用两个 merchant 的测试数据证明列表、详情、下载、重试、选择版本均不能串商家。
- [ ] 后台任务和恢复器按记录取得商家上下文。

### Task 5：AM 素材读取与 Azure 存储

- [ ] social 同源 API 包装 AM search／get，支持分页、搜索、明确错误。
- [ ] 临时图片保留先传 social 后端的链路；完成校验后由后端通过 AM 获取 SAS 并保存 original／prepared，浏览器拿不到 SAS。
- [ ] 实现持久化逻辑 key → 企业 file_key／URL 映射。
- [ ] 失败／回滚对象解除业务可见性但保留可追踪孤儿记录，不新增物理删除或产品清理接口。
- [ ] 对 prompt optimize、图片网关、Agent 输入、预览和下载统一适配。
- [ ] 服务凭证可在无员工 session 时完成已有任务的结果保存。
- [ ] 图片确认失败不得把记录标可用，文件未保存完成不得把任务标成功。

### Task 6：独立站 UI 与 AM 启动入口

- [ ] AM 复用商家搜索，支持前 100 家以外的商家。
- [ ] 用户选商家后同步按钮点击打开固定 media 域名，编码 merchant_id；不把 session／服务 token 放 URL。
- [ ] 独立站显示商家、只读资源库入口和现有临时上传入口。
- [ ] 保留工作流和深链接，空品牌状态可用。
- [ ] 停用 AM mock 工作台入口；入口正式开放放在后端／站点验收之后。

### Task 7：容器与环境配置

- [ ] social API／Web Dockerfile、忽略规则、生产静态站配置和健康探测。
- [ ] 按第 8.5 节组织 ConfigMap／加密 Secret／Deployment／Service／Envoy HTTPRoute，social 自有资源统一部署到 uat。
- [ ] 复核 Task 1A 已交接的 social-media DNS 记录及执行结果，避免重复创建旧 media 域名或夹带用户 lockfile；DNS 不应到本步骤才首次准备。
- [ ] 配置共享 Mongo、AM 地址、Core AI UAT 配置、存储服务身份和可写临时目录。
- [ ] 按第 8.4 节实现 Mongo 原子 admission、维护 CLI、完整写请求门禁和活跃任务排空，补手工发布／回滚步骤。
- [ ] 按第 8.6 节执行权限范围内的 Secret／Kubernetes 应用与镜像发布；DNS apply 交给有权限人员，分别记录已完成和等待项，不能只提交文件就报告整体已部署。
- [ ] 初次资源创建需要有相应权限；不要把 Jenkins 现有 update 权限当作 create 权限。

### Task 8：验证、发布与交接

- [ ] 执行 agent 自行运行补充离线／集成检查，并使用 Codex 内置浏览器逐条完成 SM01–SM15 页面冒烟用例；发现问题自行定位、修复并重跑受影响验证，不把可自行完成的测试交给用户。
- [ ] 先部署兼容的 AM API，再部署 social，最后开放 AM 启动入口。
- [ ] DNS 权限人员完成 apply 后，agent 亲自验证解析／HTTPS 并继续页面验收；等待 DNS 时仍推进可做工作，实际域名用例明确标阻塞。
- [ ] 按测试商家范围完成真实模型、文件落 Azure、商家隔离、后台身份、恢复与维护发布验收，不能仅调用 mock 或读取健康接口。
- [ ] 记录镜像 tag、实际域名、数据库名、配置项名、测试结果、限制和回滚方式；不记录密钥值。
- [ ] 每个仓库单独审查 diff；按项目规范提交，不混入无关工作。
- [ ] 交付第 10.4 节规定的验收报告与证据，按第 10.5 节完成条件核对；缺少权限／验收资源则明确标为阻塞，不能把未执行项目算通过。
- [ ] 按第 14 节使用 TWG 将实际修改与验收结果回写 FBR-7713，上传／关联可审阅的报告和关键证据，并原生回读确认；记录评论／附件链接。

---

## 10. Codex 内置浏览器真实页面测试与自助验收（强制完成条件）

### 10.0 责任与执行闭环

**用户已明确要求：写完代码后，由执行 agent 使用 Codex 内置浏览器，在真实页面自助测试、自助验收；不是简单的单元测试。** 必须按照[独立冒烟用例 SM01–SM15](2026-11-02-media-uat-browser-smoke-cases.md)逐条执行，记录具体输入、页面步骤、预期、实际结果与证据。本节不是留给用户执行的待办清单。

执行闭环：实现 → 离线测试／构建 → 真实依赖集成 → UAT 部署 → 浏览器完整业务流程 → 权限／故障／恢复验证 → 修复失败 → 重跑受影响项 → 交付验收报告。

- 先取得当前基线，再验证改动；失败必须区分基线问题、本次回归、模型／外部依赖错误，给出具体证据。
- 用户已确认的业务选择不重问。测试脚本、数据准备、日志分析、浏览器操作和结果检查由 agent 自行完成；仅在缺少实际账号登录、云权限、测试商家或额度等外部条件时向用户说明具体所缺内容。
- 页面验收工具固定为 Codex 内置浏览器，本会话入口为 mcp__cua_repl 的 in-app browser（iab）；执行时按该工具初始化文档操作。主流程必须真实点击、输入、通过文件选择器上传、提交生成、查看版本与点击下载。
- pytest／JUnit／Vitest／Playwright 仅作为补充检查。不能用外部浏览器、browser-act、只运行 Playwright 脚本、API mock、直接 POST 造任务或一张首页截图，替代内置浏览器的真实页面操作。若执行环境缺少内置浏览器，记录环境阻塞，不自行降低验收标准。
- 仅使用明确的 UAT 测试 merchant 和带可识别标记的测试数据；不删除 AM 原资源，不整体清理 merchant media 前缀，不影响无关应用或共享 Mongo。
- 正常用例通过后不重复无意义地全量回归；修复后重跑受影响测试及必要完成检查。新增权限、并发、恢复和文件归属逻辑必须有针对性测试。
- 所有结果都按实际执行记录：通过、失败、阻塞、不适用。必需项目未执行就是未完成，不能以“建议后续手测”或“应该可用”作为最终验收结论。

### 10.1 补充离线验证命令（不能替代页面验收）

social Python（工作目录 D:/java/DRI/social-media/backend）：

~~~powershell
./.venv/Scripts/python.exe -m pytest
~~~

social 前端（工作目录 D:/java/DRI/social-media/frontend）：

~~~powershell
npm run build
npm run lint
~~~

AM Java（工作目录 D:/java/DRI/fbr-project；遵循适用 Gradle 检查技能／规范）：

~~~powershell
./gradlew.bat :frontend:am-console-site:test
./gradlew.bat :frontend:am-console-site:check
~~~

AM 前端（工作目录 D:/java/DRI/fbr-project/frontend/am-console-site-frontend）：

~~~powershell
pnpm test:run
pnpm build
pnpm lint
~~~

按新增接口同步类型；先检查现有 pnpm api 脚本的数据源和副作用，不盲目覆盖生成类型。若基线检查本来失败，记录基线与本次新增问题，不声称全部通过。

集成验证还必须包含：

- 用本地／隔离测试实例运行 Mongo 7 并启用与 UAT 一致的 notablescan，执行真实查询与索引验证；记录代表性 explain。mongomock 只用于离线单元测试，不能代替该集成验收。
- 对两个 merchant 的七个 social 自有集合及新文件映射验证 merchant_id，检查父任务、版本、品牌、资产归属一致；brand_rules 每商家一条，默认品牌和 hash 去重互不影响。商户资料另测 AM 读取及字段投影／多门店、缺资料与上游错误区分、刷新与 AM 一致、保存／复制风格归属保留、跨商家 profile 拒绝；不依赖本地资料种子。复核背景替换自然语言＋有序 URL、最终图识别、已有 run 原截止时间内恢复查询的现有测试。
- 模拟 AM 无会话、无权限、超时、服务凭证错误，以及 Blob PUT／stat／版本提交失败；证明拒绝访问、错误状态和孤儿追踪符合预期。
- 对维护 enable 与并发提交做确定性的自动化测试，覆盖同步付费提示词优化、上传和没有任务记录的请求；证明 wait 不会在请求即将落库时提前报告排空。
- 构建实际 API／Web 容器并启动检查，验证非 root、只读根目录、临时卷、单 worker、静态站路由和 readiness／liveness；只在开发服务器跑通不算容器验证。
- 环境文件通过适用的 Terraform fmt -check／validate／plan、Kubernetes 服务端 dry-run 或等价校验；计划差异只含本次预期资源，不夹带既有 lockfile 变更。需要登录／权限的检查不得伪造成功。

### 10.2 必测行为

| 类别 | 验收点 |
|---|---|
| 身份 | 未登录拒绝；当前 session 有效可进入；AM 退出后页面 API 拒绝继续访问 |
| CSRF | 精确 media Origin 和 X-Media-Request 缺一拒绝写操作；覆盖其他同父域、缺失／null Origin、multipart 请求；后台服务凭证接口单独验证 |
| 商家权限 | primary AM／实时管理员放行，其他员工拒绝；浏览器改 merchant_id／素材 id／task id 不能越权 |
| 多标签页 | 两个商家分别开页，素材、品牌、任务、下载互不污染 |
| 商家选择 | 支持搜索、分页，能选第 100 家之后的合法商家 |
| 资源库 | 只读查询，能选用；没有写回、改名、删除或候选自动入库 |
| 临时图片 | 不入 AM 库，大小／格式／像素按实际字节校验；准备图与原图分开 |
| 上传链路 | 浏览器先传 social，后端向 AM 取 SAS；浏览器响应、页面和模型快照不含写入 SAS，完成后不得用原 key 覆盖历史图片 |
| 保真 | 宽度超过 1920 的图不被 file-service 默认压缩；透明 PNG 保持透明；背景替换使用正确原始尺寸 |
| 存储归属 | 全部 social 自有文件进入 merchantId/media；伪造其他 merchant、其他目录、非法 kind/key 拒绝 |
| 持久化 | 清空 Pod 缓存／重启后仍可读取已有图片、版本和品牌引用 |
| 失败文件 | Blob 上传成功但确认／版本提交失败时，资产不对业务可见；保留 merchant／file_key 的孤儿追踪，原始错误仍可见 |
| 后台身份 | 用户关页、AM 注销／会话失效后，已提交且可继续的任务仍能保存 |
| 恢复 | 独立改图／背景替换继续原 run，不重新 trigger；其他中断按已确认限制展示并可人工重试 |
| Mongo | 使用真实 UAT 同类 Mongo 7、notablescan 验证代表性查询／explain；mongomock 通过不能替代该检查 |
| 发布 | 维护时拒绝新写操作和付费优化、旧任务可查询；并发提交与启用维护无排空竞争；无新旧 API 进程重叠；更新后恢复接单 |
| 初始状态 | 新 UAT 商家无旧数据；品牌空状态可操作，通用规则属于当前商家 |
| 保密 | 浏览器拿不到服务凭证，URL／日志／模型输入快照没有员工 cookie／后台凭证 |

### 10.3 内置浏览器逐步冒烟用例与 UAT 验收

agent 在指定 UAT 测试资源上使用 Codex 内置浏览器自行验收。具体可执行步骤在[页面冒烟用例](2026-11-02-media-uat-browser-smoke-cases.md)，包含前置条件、固定测试数据、每一步操作与预期、证据和通过条件。下面 U01–U11 为覆盖范围总表，不能拿覆盖总表替代 SM01–SM15 的实际步骤记录。

| 编号 | agent 必须实际执行的步骤 | 通过条件／证据 |
|---|---|---|
| U01 部署入口 | 检查 HTTPS／证书、uat 中 API／Web 镜像 tag、Pod、探活、HTTPRoute parentRefs／hostnames／路径／状态及后端 Service | 正式域名打开生产静态站；/api 和 / 路由正确；镜像与验收提交一致，不能用本地 Vite 页面冒充 UAT |
| U02 AM 跳转 | 在 AM 登录，搜索选择 merchant，点击打开 media；刷新、切菜单、打开历史任务 | 商家正确且不可站内切换，merchant_id 与有效任务深链接始终保留；无多余登录流程 |
| U03 资源与品牌 | 浏览／搜索／翻页选择真实 AM 图片；临时上传；创建、编辑及复制风格与规则，选择 Logo／参考图，对照 AM merchant-profile 查看商户特点及来源 | 素材来自当前 merchant；临时图与品牌保存可回读；资料来自 AM、原文字段及门店归属正确，AM 无资料显示待补充；没有写回 AM 资料或新增本地主档；AM 资源库没有被新增／删除；空品牌状态可用 |
| U04 提示词和整图 | 完成一次真实提示词优化和整图生成，查看任务、候选、输入快照并下载 | 调用真实 UAT Core AI；结果是有效图片、可预览下载；Mongo 记录和 Azure 对象可定位，不能只看 succeeded 字样 |
| U05 整图继续编辑 | 基于生成版本执行一次工作台改图，查看网关／Agent 分支、选择结果并重新打开任务 | 分支状态、版本来源、输出选择和下载正确；历史版本不被新结果覆盖 |
| U06 独立改图／背景替换 | 各执行一个最小真实候选，检查原图、替换素材、最终图和任务中心入口 | 两个工作流均走通；背景替换结果尺寸符合原始图片要求；透明 PNG 与成品按既有规则保留，输入原始字节没有被普通文件上传压缩 |
| U07 权限与双标签页 | 用两商家、普通 AM／管理员验证；改 URL／task／asset／version ID；检查无登录和注销后的页面请求 | 合法访问通过，越权被拒绝，两个页面数据不串；CSRF／服务凭证的真实请求行为符合第 10.2 节，不产生越权任务或对象 |
| U08 后台身份与恢复 | 提交一个可恢复 Agent 任务，保存 run_id 后关闭页面／注销 AM；在只有测试任务的受控窗口重建 social API 进程，再登录查看 | 后台使用服务身份继续同一 run 并保存结果；无重复 trigger；图片映射在新 Pod 可读取。其他不支持恢复的任务按已确认范围显示失败并可人工重试 |
| U09 Azure 与持久化 | 核对上传原图 hash、宽图／透明 PNG、企业 file_key 前缀，清空可重建缓存或重建 social Pod 后再下载 | 文件属于目标 merchant/media，原始内容／尺寸／透明度正确；原图、版本和品牌引用不依赖旧 Pod；浏览器拿不到服务 key／写入 SAS |
| U10 维护发布 | enable 维护，并发尝试写请求与付费优化，等待排空，更新镜像／重建，确认初始化后 disable | 新写入拒绝、读与下载可用、既有任务可保存、wait 不提前结束；无新旧 API 重叠，维护状态跨重启保留，恢复后可提交 |
| U11 资源与回退 | 观察 1–3 人／多候选代表性负载的 CPU、内存、线程和任务状态；执行适用的回退演练 | 没有 OOM／错误串商家／重复执行，资源设置有观测依据；回退过程保留 Mongo 和 Azure 数据，报告准确说明实际演练范围 |

执行细则：

- 真实模型验收采用最少必要调用，各工作流先用一个候选。候选上限、异常和并发可优先用受控模型替身做自动化测试；报告明确标注 mock，不能当作外部模型额度／吞吐的实测证据。
- 已经获得 run_id 的请求以查询／恢复为主，不为了刷新结果再次 trigger。模型或外部服务失败时先查状态与日志，再决定受影响场景是否需要重测，避免重复费用。
- 运维故障注入只影响本应用，使用无其他用户任务的测试窗口；不重启 AM、共享 Mongo、Gateway 或整个节点池。
- 首次发布没有旧镜像时，实际验证关闭入口／暂停接单和数据保留，标注“尚无上一版本，未演练旧镜像回退”；后续存在上一可用 tag 时再验证镜像回滚，不虚构已演练。
- UI 验收检查成功、空数据、错误和权限拒绝状态，检查浏览器 console／network 的实际错误；明显页面缺陷由 agent 修复后重测。
- 图片验收既检查文件有效性、像素尺寸、原图 hash 和透明度，也通过缩略图或独立短上下文检查实际结果，遵守 AGENTS.md 的大图安全规则。仅取得下载链接不算验收图片内容。

回滚保留上一个 API／Web 镜像 tag，采用相同维护流程；先关闭新入口／任务提交，再回旧镜像。不通过删除 Mongo 或整个 media 前缀“回滚”。AM 新接口应尽量向后兼容，不影响现有资源库／附件流程。

### 10.4 必交验收报告与证据

agent 创建 docs/plans/<实际验收日期>-media-uat-am-console-verification.md，并从本文链接到报告；更新第 9 节清单时只勾选实际完成项。

报告至少包括：

1. 实际执行时间、执行环境、三仓库 HEAD、API／Web 镜像 tag、域名与 namespace、测试 merchant／角色标记；不保存密码、cookie、服务 key 或签名上传 URL。
2. 每条测试命令的工作目录、退出码、通过／失败数量及可定位的完整输出路径；新失败的原因、修复和复测记录。
3. SM01–SM15 的逐步记录，以及第 10.2 节／U01–U11 覆盖结果；不用一句“自测通过”替代。结果表必须记录内置浏览器标签页／页面、真实点击与输入、预期、实际结果、状态和证据路径；mock 检查单独列为补充。
4. 内置浏览器关键步骤截图、真实下载路径、经过脱敏的辅助 API 响应、Mongo 查询／explain、文件 hash／尺寸／key、Pod／路由状态和维护／恢复日志。辅助证据不能代替页面操作记录；原始日志／截图按 gitignore 规则存放。
5. 仍未执行或失败的必需项、具体阻塞条件、已完成范围、剩余动作及影响；“没有权限”应注明具体命令／服务与错误，不能以此掩盖可继续完成的本地工作。
6. 最终结论与回退方案：全部必需项通过才写“实现与自助验收完成”；否则写“代码／离线检查已完成，UAT 验收阻塞／存在失败”，明确未完成部分。
7. FBR-7713 的实施汇总评论、报告附件／证据链接，以及 TWG 回读确认结果；Jira 上的结论应与本地报告一致。

### 10.5 完成判定

只有同时满足以下条件，执行 agent 才能将整体任务报告为完成：

- [ ] 已确认需求及三仓库改动实现，并通过 diff 审查，未夹带用户原有改动或密钥。
- [ ] 必需离线测试、构建／静态检查、真实 Mongo 与容器／环境校验实际执行并通过。
- [ ] 使用 Codex 内置浏览器实际执行 SM01–SM15，真实 AM／Azure／Core AI 页面链路通过；商家隔离、后台身份、恢复和维护等关键场景有逐步证据，不能用补充测试替代。
- [ ] 验收发现的本次缺陷由 agent 修复并重测；没有未说明的必需失败／未测项。
- [ ] 验收报告、证据、实际配置／镜像信息和可执行回退步骤已交付。
- [ ] 通过 TWG 将实际工作和验证结果回写 FBR-7713，并回读核对；若回写受阻，明确记录“实现／验收状态”和“Jira 回写阻塞”，不能声称整个交付已完成。

缺少外部账号、登录、云权限或额度时，agent 继续完成不依赖该条件的工作，给出准确的阻塞说明；不得擅自跳过，也不得把完整验收默认为用户负责。产品人员之后的主观体验反馈不替代上述自助验收。

本次已知的 DNS 权限分工按第 8.6 节执行：有权限人员只承担域名 plan／apply，agent 仍负责其后的 DNS／HTTPS 检查和真实页面验收，不能把整份验收交给该执行者或用户。

---

## 11. 本次已经验证与没有验证的内容

### 已完成

- 只读审查 social-media、AM Console、operation API、file-service、UAT 部署／DNS／存储配置及相关依赖实现。
- 确认 UAT 参考项目配置、AM 资源接口、SAS 复用链路和商家规则。
- 历史 DNS 实查：中途旧候选域名 media.connexup-uat.net 当时不存在；influencer-outreach.connexup-uat.net 解析为共享 Gateway 地址 20.1.217.183。旧 media 域名已被用户放弃，不作为部署目标。
- 用户改名后重新只读查询：social-media.connexup-uat.net 也返回 DNS 名称不存在；仓库当时没有该记录。实施时重新查，不硬编码 Gateway IP，不把尚未 apply 写成已上线。
- 历史 Azure CORS OPTIONS 预检（使用中途旧 media Origin，不等于新 social-media 域名已验证；最终链路由后端上传，也不依赖该浏览器预检）：
  - Origin=https://media.connexup-uat.net
  - Access-Control-Request-Method=PUT
  - Access-Control-Request-Headers=content-type,x-ms-blob-type
  - 返回 200，Allow-Origin 为该 media 域名，Allow-Methods=PUT，两个请求头均允许。
  - 仅做 OPTIONS，没有创建或上传 Blob。
- 核实当前 AM Service 仅 8080、AM cookie 与服务端 session 行为。
- 前期文档整理阶段仅新增交接文档；后续已按用户单独指令准备 DNS 配置，最新未提交／未 apply 状态见本文开头与 Task 1A。业务功能和部署仍未实施。

### 未完成／不能当作已经完成

- 没有部署任何资源、创建 DNS、写入 Secret 或执行 Terraform apply。
- kubectl 实时查询要求 Microsoft 重新登录，已停止查询；没有核实线上 Pod、实际镜像、配置或当前容量。
- 没有实际使用员工 session 调 AM。
- 没有真实上传、删除、模型生成、数据库迁移或清理。
- 没有为未实现的改动运行测试；本文命令与用例是实施后的验证要求，不是通过记录。
- 新 AM 服务身份／商家授权／媒体存储接口尚未实现。

---

## 12. 可以直接交给另一个 agent 的执行提示词

~~~text
请在 D:/java/DRI 完成本任务的代码开发、UAT 部署和真实页面自助验收。先完整阅读 D:/java/DRI/social-media/docs/plans/2026-11-02-media-uat-am-console-handoff.md，以及 D:/java/DRI/social-media/docs/plans/2026-11-02-media-uat-browser-smoke-cases.md；按已确认方案持续实施到交付，不止停在分析或计划。

关联 Jira 为 FBR-7713：https://chancetop.atlassian.net/browse/FBR-7713。先用 TWG 读取当前工单，按本文和用户后续明确指示实施。各仓库提交／PR 带上该工单关联，遵循各自提交格式。

先读全文、D:/java/DRI/AGENTS.md 和各目标目录的局部指令，查看三个仓库 Git 状态；fbr-project 定位代码先用 CodeGraph。该文档是 2026-11-02 本次对话的最终交接，优先于 social-media/docs/analysis/java-image-domain-api-design.md 中的旧 Java 迁移方向。

保留独立 React/FastAPI 应用，目标 https://social-media.connexup-uat.net；social API／Web 及配套资源明确部署到 UAT 环境的 uat namespace，与 AM 相同，不沿用参考应用的 uat-ai。AM 选 merchant 后跳转，沿用 AM 权限，全部业务数据按商家隔离。素材读取和上传签发统一走 AM Console API，不用 operation API。social 文件统一放 Azure 的 /fbr-uat/product/<merchantId>/media/，不进 AM 资源库；本期不做清理或历史迁移。域名改名不改变此文件目录。

用户明确商户特点就是 AM merchant-profile，对应 ai-marketing 的 restaurant_brand_profiles，_id 为 merchantId。social 后端先验授权，再读 AM GET /ajax/merchant/:merchantId/profile 并适配展示；不使用独立 merchant_profiles、不导入 seejoy 种子、不直连 ai-marketing Mongo 或绕过 AM。平台视觉风格仍在 social；资料只读，不自动加入生图提示词。第 4.5、7.1 节覆盖此前增加第八个本地主档的建议。

主验收商家最终为 UMETEA（UME），第二商家为 choice brooklyn；此前 SEE JOY 主商家决定已被用户撤销，本轮不访问或测试其资料。测试风格卡片归 UME，样例图片保持原件，使用 storage/verification/input-assets/海报提示词-UME测试.txt（原文中 SEE JOY 替换为 UME 的副本）；原提示词保留不改。AM 商户特点仍读两家真实资料，不把测试风格内容写回 AM。两家已在指定账号的 AM 页面打开资料，实际 merchant_id 在正式入口／正常响应中记录，不按名称或本地种子猜测。

本机执行输入已经准备：Core AI Key 在 D:/java/DRI/social-media/backend/.env；AM 测试登录配置在 D:/java/DRI/social-media/.env.uat-smoke；素材与 UME 提示词在 D:/java/DRI/social-media/storage/verification/input-assets/。读取最新本地值使用，凭证文件保持 Git 忽略并排除出 Docker 构建上下文，不写进代码、文档、日志、截图或 Jira。两家商户的页面可见性已预检；实际角色／owner 及模型调用权限仍按真实结果核实，不将单一账号伪装成两种角色。

入口按用户最终决定参考 outreach：DNS 引用 azurerm_public_ip.fbrdev_envoy_gateway_ip.ip_address，HTTPRoute 在 uat 中，parentRefs 指向 envoy-gateway-system/internal-gateway、sectionName=https；复用现有 listener 的 uat/fbr-uat-net-tls 证书，/api 到 API，/ 到 Web。不要为 social 配置 Application Gateway Ingress；分别核对探活与同步优化请求超时。最新磁盘配置已匹配 Envoy，DNS 经本机／Google／Azure 权威验证已解析至 20.1.217.183，原 DNS 阻塞解除；应用部署、HTTPS 和内置浏览器验收仍待完成，接手时复核实时状态，不重复建 DNS 或自行 apply。

临时图片已明确选择先传 social 后端，校验后由后端向 AM 取 SAS 并上传 Azure；不把 SAS 交给浏览器。商家 API 统一采用 /api/merchants/{merchant_id}/...，cookie 写请求检查精确 Origin 与 X-Media-Request。后台媒体接口采用 Bearer 服务凭证，配置项 MEDIA_STORAGE_SERVICE_KEY。失败自有 Blob 保留可追踪孤儿，不增加物理清理。

为后台配置受限服务身份，保证已有可恢复任务不依赖员工登录继续保存；首期单 Pod、单 worker、维护窗口手工发布，保留当前恢复能力，不扩成所有工作流无损恢复。

初始化按第 8.3 节由部署到 AKS 的 social 后端执行：自身应用库的索引与缺失控制记录先初始化，再恢复任务并 readiness；重复启动不覆盖已有维护状态或商家规范。通用规范缺失时返回代码默认，首次正常保存按商家落库；测试风格、上传资产和任务在浏览器中通过正常 API 创建。无需本机直连 UAT Mongo，不导入本地历史或 AM 商户主档。

DNS 已有生效快照，接手先只读复核，不再重复新增或无条件等待 DNS；若后续确需调整，仍交用户安排的有权限人员 plan／apply，agent 不自行执行 Terraform apply 或擅自联系／分派他人。应用／HTTPRoute／HTTPS 尚待实施，解析成功不能当作应用上线或浏览器验收通过。

按文档任务顺序实施，保护已有未提交改动，测试真实权限边界和存储保真，分别记录离线／UAT 验证。不要重新询问已确认的产品决策；发现新的真实冲突或缺少外部访问条件时说明具体事实。新增接口名称是实施建议，不要误认为仓库已存在。每个仓库独立审查和提交，勿夹带无关文件或密钥。

你还负责第 10 节规定的自助测试和自助验收：必须使用 Codex 内置浏览器，按 D:/java/DRI/social-media/docs/plans/2026-11-02-media-uat-browser-smoke-cases.md 的 SM01–SM15，真实点击、输入、上传、生成、查看任务与下载，验证 AM→media→Azure／Core AI。单元测试、外部浏览器、Playwright 脚本和直接 API 请求都不替代这些页面步骤。修复发现的问题后在内置浏览器复测，交付逐步操作、预期／实际、截图及真实下载证据。必需项未执行不能宣告完成；若内置浏览器、外部登录／权限／测试账号／额度不可用，完成其他可做工作并明确记录阻塞，不把自助验收交还用户。

用户已明确要求使用 TWG 把实际做了什么、三仓库改了什么、提交／PR、页面测试过程和证据、部署／回退及未完成项写到 FBR-7713。执行后按第 14 节创建或维护一条完整汇总评论，附报告及必要证据，并回读确认，给用户可直接 review 的 Jira 链接；不以本机文件路径作为唯一交付，也不把计划写成已完成。当前授权不包含擅自关闭工单、改负责人或覆盖原需求。
~~~

## 13. 独立 subagent 审阅记录（2026-11-02）

用户要求独立审阅后，已由一个只读 subagent 对本文、AM／file-service、social 存储／恢复代码和 UAT Gateway 配置进行复核；未实施业务代码或部署。

| 发现 | 交接文档的处理 |
|---|---|
| cookie 写操作缺少明确 CSRF 边界 | 固定精确 Origin 与 X-Media-Request 防护，补验收用例 |
| 浏览器 SAS 的 write/create 权限在完成校验后仍可覆盖原对象 | 已再次询问用户，用户明确选择先传 social 后端；撤销旧浏览器直传默认 |
| 新 AM init／complete 与旧 ObjectStore.delete 语义不完整 | 固定失败对象逻辑脱离、保留可追踪孤儿，不增加物理清理接口 |
| 商家传递、服务凭证、维护门禁仍有技术二选一 | 固定商家路径、Bearer／MEDIA_STORAGE_SERVICE_KEY、Mongo 原子 admission 与维护 CLI |
| 把配置审阅／OPTIONS 成功当作实际部署验证的风险 | 保留事实与未验证清单；上线前验证运行状态与真实上传 |

上述为**文档已澄清、实现仍待完成**。当前没有需要重复确认的产品方向；最后一项临时上传选择也已获得明确回答。

上线前仍需准备外部条件，开发可以先进行：

- 明确应用首发执行者及 DNS 有权限执行者；应用执行身份核实 ACR、AKS uat 和 SOPS 权限，域名 plan／apply 按已确认分工交接，不要求 agent 取得 DNS 写权限。不在聊天或文档粘贴密钥。
- 准备至少两个 UAT 测试 merchant、普通 AM 与管理员验收身份；真实账号信息在执行时通过安全渠道或既有登录使用。
- 确认现有 Core AI UAT 凭证、Agent／模型可用及验收额度；没有真实调用结果前不声称模型验证通过。
- 实查域名、Pod／镜像、节点容量、AM HTTPS／会话验证、Mongo 查询与 Blob 存取。这些是部署前验证项，不是再次询问已定业务决策。

### 13.1 用户需要补充的信息与已能从仓库取得的信息

用户不必重新整理所有项目地址。AM UAT 地址、social-media 目标域名、共享 UAT Mongo 的集群内拓扑／replicaSet、ACR、Blob、Envoy Gateway／DNS 配置和 Core AI UAT 默认地址／Agent ID 已有代码或环境配置依据，执行 agent 先自行读取、核实。

| 需要用户补充的内容 | 最小信息／配合方式 |
|---|---|
| 测试商家 | 用户最终指定 MA=UMETEA（UME）、MB=choice brooklyn，已通过内置浏览器打开两家真实资料，选择已确认。实际 merchant_id 在正式入口／正常响应中记录；SEE JOY 可见性已不再是本轮前置，不再追问或测试该商家 |
| AM 验收身份 | 用户指定 amconsole@chancetop.com，本轮已在内置浏览器登录成功；密码仅保存在本机被 Git 忽略的 .env.uat-smoke。实际角色／owner 范围仍须核实，不能把单一账号假定为两种角色；确需用户操作时仅请求登录／MFA 或缺失角色条件 |
| Core AI UAT 凭证来源 | 用户已提供 Key，并说明后续会轮换；已保存至本机被 Git 忽略的 backend/.env 的 CORE_AI_API_KEY。外部调用权限／额度及 UAT Secret 注入尚未验证，不将 Key 写入本文、Git 或 Jira |
| 首发访问 | 应用部署身份完成必要 Azure 登录，并核实 ACR、AKS uat 与 SOPS 权限；DNS 由用户安排有权限人员 plan／apply，提供执行结果供 agent 后续验证 |
| 团队另有指定配置（如有） | 若 Mongo／AM／Core AI 地址与仓库中的 UAT 配置不同，提供新的非敏感地址或配置来源；否则沿用既有依据，不要求额外提供公网 Mongo |

2026-11-02 先前检查时 backend/.env 尚不存在，进程／Windows 用户环境未配置 CORE_AI_API_KEY；随后用户提供凭证，现已建立本机 backend/.env，覆盖此前“缺少本机 Key 来源”的状态。UAT Secret 是否已有配置、模型／Agent 实际调用权限与额度仍须验证；记录配置位置，不输出凭证。

以上不阻塞先开发和离线测试。真实模型、完整页面权限验收和发布前需要相应资源到位；不能在资源缺失时伪造通过结果。

### 13.2 发布与验收的权限清单及当前实查状态

以下是实际执行所需能力，不代表全部缺失或需要新开权限。优先核实当前身份／现有配置，按应用和目标 namespace 使用；缺失时再明确请求相应配合。

| 范围 | 所需能力／用途 | 当前状态与分工 |
|---|---|---|
| AKS uat | 创建／更新本应用 Deployment、Service、ConfigMap、Secret、HTTPRoute，更新本次涉及的 AM Deployment／配置；读取 Pod、日志、事件和路由状态，执行本应用受控更新／恢复验收；共享 Envoy Gateway 只需检查现有配置 | 已配置 context=FBRDevAKSCluster；实际 namespace 权限尚未验证，见下方认证检查 |
| ACR | 向 fbrdevacr 的目标 UAT 镜像仓库发布 social API／Web 镜像，并按既有方式发布受影响 AM 镜像；若使用远端构建，另需相应构建能力；AKS 运行身份能拉取对应镜像 | 发布身份与 AKS 拉取能力分别验证，不能把“能看仓库”当作“能推镜像”；尚未实测 |
| SOPS／Key Vault／Secret | 使用仓库配置的密钥加解密本次应用与 AM 的必要 Secret，并在 uat 注入；只处理本次所需配置 | .sops.yaml 指向 fbrdevkv 的 FBRDevSecretsEncrypt；密钥使用权限尚未验证，本轮未解密或输出 Secret |
| social Mongo 应用库 | Pod 可连目标集群，数据库身份具有自身库读写与建索引权限，执行第 8.3 节初始化 | 尚未从 UAT 验证；若需配置权限由负责 Mongo 的人员处理，不要求本机公网直连 |
| Core AI UAT | 可调用当前图片模型、优化 Agent、图片 Agent、背景替换 Agent 的有效凭证及可用额度 | 用户已提供并保存至 backend/.env；尚未验证模型／Agent 权限、额度或部署到 UAT Secret，后续使用轮换后的最新凭证 |
| AM 页面验收 | 能正常登录并访问 UMETEA 和 choice brooklyn；普通 AM／管理员／非 owner 分支按实际角色覆盖，必要时补齐相应测试条件 | 指定账号已登录并打开两家资料；角色／owner 范围尚未核实，不将两家可见当作所有权限用例均已通过 |
| social 后台存储身份 | 为新媒体 init／complete 接口生成独立受限服务凭证，并配置到 AM 和 social；员工登出后仍可保存既有任务结果 | 是本次开发配置项，不要求用户提供员工 cookie 或把 AM 管理员身份交给后台。底层 Blob SAS 继续由现有 AM／file-service 身份签发，social 不另配 Azure Storage 管理凭证 |
| Git／Jira 交付 | 按用户后续指示向涉及仓库提交／推送并创建 PR（如采用）；TWG 对 FBR-7713 可发表评论、上传验收附件 | social 拉取已成功，推送／PR 权限未实测；TWG 读取该工单此前成功，评论／附件写权限尚未实测。保留当前 DNS 先审阅、未经后续指示不自动提交的约束 |
| DNS Terraform | 在既有 backend／workspace 下读取计划并应用本次 A 记录 | 继续由用户安排有权限同事执行；agent 负责配置交接和后续解析／HTTPS 验证，不要求给 agent DNS 写权限 |

2026-11-02 只读认证检查：本机具备 az、kubectl、terraform、sops、docker。az account show 返回默认订阅名 fbr-dev、state=Enabled；kubectl 当前 context 为 FBRDevAKSCluster。随后执行显式 context、namespace=uat 的 auth can-i --list 时，凭证插件要求企业设备登录，尚未返回授权结果；agent 已中止该等待命令，没有完成登录、改变 context 或执行部署。Azure CLI 的账号配置可读不等于 AKS 凭证有效，也不等于已确认具备目标资源权限；此处属于“需重新认证，授权待核实”，不能报告为已获权限或权限被拒绝。

执行阶段首先在用户可完成交互的条件下恢复 AKS 登录，再做只读 namespace 权限核查。Core AI 凭证从受控 Secret／本地安全配置注入，AM 登录由内置浏览器正常完成；不要求在聊天或 Jira 贴密码、token、cookie 或解密内容。其余权限按上表核实，已有可用身份／凭证无需重新申请。

### 13.3 用户补齐执行输入与 AM 页面预检（2026-11-02）

- 用户最终指定 MA=UMETEA（UME）、MB=choice brooklyn。两家均已在 Codex 内置浏览器打开资料；UMETEA 的 profile canonical name 为 George Merchant，choice brooklyn 为 Choice Brooklyn。保留真实差异，以 merchant_id 确定归属，不改写或合并 AM 资料。用户要求样例风格卡片视作 UME 的测试卡片，生成使用品牌名改为 UME 的本地提示词副本。
- Core AI Key 已保存于 D:/java/DRI/social-media/backend/.env；AM 测试账号 amconsole@chancetop.com 的登录配置保存在 D:/java/DRI/social-media/.env.uat-smoke（AM_SMOKE_URL／AM_SMOKE_USERNAME／AM_SMOKE_PASSWORD）。两个文件均验证被 Git 忽略；不包含在提交、文档附件或 Jira 中。新执行机需要另行安全注入，轮换后读取最新值，不依赖本次对话中的旧值。
- 已通过正常 UAT AM 登录页面实际登录成功，列表可见 Chancetop、KEKE Food、UMETEA、choice brooklyn、onlybear。此前搜索不到 SEE JOY 后曾请求核实；用户认为其较敏感，明确改用 UME／choice brooklyn，该旧问题已撤销，不再阻塞或追问。未更改商家分配、账号角色或 AM 主档。
- 上述凭证文件仅供本机执行使用；实现 Dockerfile 时同时通过 .dockerignore 排除 .env／.env.*，不把它们复制进镜像。UAT 通过既定 SOPS／Secret 注入最新值，前端不接收服务 Key 或登录密码。
- 指定账号的实际角色尚未验证。它可用于符合其真实权限的用例；普通 AM／管理员／非 owner 的各分支按实际身份覆盖，条件不满足时如实记录并补齐对应测试条件，不能由同一账号伪装不同角色。
- 此次是 AM 登录和商家可见性预检，不代表 SM01–SM15 已执行或通过；social 业务实现／部署／真实模型调用仍待后续完成。主／次商家及本机凭证来源已明确，后续按实际角色、部署权限和模型能力完成验证。
- 预检截图保存在 D:/java/DRI/social-media/storage/verification/2026-11-02-am-precheck/am-merchants-choice.jpg，画面为已登录 AM 的商家列表及 Choice Brooklyn 资料，不包含登录密码或 Key；仅作为此次预检证据。

## 14. FBR-7713：使用 TWG 回写实施与验收结果

### 14.1 工单与授权范围

- 工单：[FBR-7713](https://chancetop.atlassian.net/browse/FBR-7713)，标题 social-media deploy to uat。
- 2026-11-02 已通过 TWG 原生读取确认：当时状态“待办”，description 为 null。执行和写入前重新读取，不能把该快照当作永远不变的状态。
- 用户已明确授权后续通过 TWG 记录本次实际工作和修改；回写评论、附审阅报告／关键证据无需重新询问是否可以发表评论。
- 回写放在实际实施与验收之后；阶段中若记录进度，必须清楚标阶段／未完成。当前文档整理不代表业务代码已实施、测试已执行或部署已成功。
- 主要使用汇总评论、附件和真实 PR／提交链接，不覆盖用户原需求；不凭本次指令自行更改工单状态、负责人、Resolution 或宣布工单关闭。

### 14.2 用户可集中 review 的内容

最终在工单维护一条完整的“FBR-7713 实施与页面验收汇总”，至少包含：

| 部分 | 必写信息 |
|---|---|
| 总体行为 | 这次完成了什么，AM 如何跳转 media，商家隔离、素材读取及文件保存的最终行为 |
| social-media 改动 | 关键模块／文件、七个自有集合的 merchant_id 改动、商户资料改走 AM profile API 的投影与授权、文件映射、前端／API、存储适配、恢复与维护门禁；说明修改原因和结果 |
| fbr-project 改动 | AM 启动入口、商家授权、媒体 SAS init／complete、后台服务身份；实际新增接口与请求／响应要点 |
| fbr-env-project 改动 | uat 资源、镜像 tag、ConfigMap／Secret 配置项名称、DNS／Gateway、探活／资源设置、发布与回退方式；绝不写凭证值 |
| Review 链接 | 每仓库分支、实际提交 SHA／链接、PR／diff 链接（如有），便于从汇总跳到真实修改；不存在的 PR 不编造 |
| 页面测试过程 | 明确使用 Codex 内置浏览器；SM01–SM15 逐项通过／失败／阻塞、真实操作与发现的问题、修复及复测、关键截图和实际下载校验 |
| 其他验证 | 自动化／集成／Mongo／容器／环境检查的实际命令、结果及证据；与真实页面验收分开，不能混用 mock 结果 |
| 实际部署 | 环境 UAT、namespace=uat、域名、实际 API／Web 镜像与验收版本、维护／恢复验证、回退状态；DNS 交接提交、实际执行者／结果和等待状态单列，未发布必须明确 |
| 遗留项 | 未实现、未验证、外部阻塞、已知限制和影响；不要把部分完成写成整体完成 |

把实施／验收报告、最终交接文档与冒烟用例作为可访问的仓库链接或 Jira 附件交付；必要时附少量关键脱敏截图。用户在 Jira 页面应能看到主要结论并打开证据，不能只留 D:/... 本机路径。大份原始日志保留可定位路径并提炼必要证据，禁止上传 .env、解密 Secret、cookie、服务 key 或 SAS URL。

### 14.3 TWG 执行规则

1. 使用 root twg 与 twg-jira 技能，先原生读取 FBR-7713 当前状态／已有评论，再准备最小回写；发现已有本 agent 的汇总评论时维护该条，避免无意义重复。
2. 写入前按当前 CLI 的 live help 核实 comment create／update、attachment upload 或关联链接契约；不猜参数。正文使用支持的 markdown／HTML 格式，不传 Jira wiki markup。
3. 长正文先保存 UTF-8 本地文件。若 CLI 没有 body-file 参数，将文件内容作为单独 --body 参数传入，不拼 shell 命令、不压成带字面换行转义的字符串。
4. 发布评论／附件后，用 TWG 原生读取核对正文、工单、链接与附件；保存评论 ID、附件 ID／可访问链接到验收报告，最终向用户返回工单／汇总评论链接。
5. 失败时区分实际鉴权／权限／服务错误与本地命令参数错误；遵循 TWG 的错误边界，不自动执行登录／安装或通过其他渠道绕过拒绝。不影响继续完成可做的开发／测试，但交付中必须明确回写未完成。

已验证可用的读取形状（PowerShell；@evidence 必须作为字符串引用，site 不带 https://）：

~~~powershell
twg jira workitem get --issue-id FBR-7713 --site chancetop.atlassian.net --fields summary,description,status,assignee --output json --output-summary auto --agent-fields '@evidence'
twg help describe 'jira workitem comment create'
twg help describe 'jira workitem attachment upload'
~~~

2026-11-02 live help 确认 comment create 支持 --issue-id、--body、--body-format markdown；attachment upload 支持 --issue-id 和可重复的 --file。此处不预执行写入，实际调用前仍以当前 live help 为准。
