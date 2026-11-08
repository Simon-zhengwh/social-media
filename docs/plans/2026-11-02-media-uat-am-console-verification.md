# Social Media UAT／AM Console 自助验收报告

日期：2026-11-02 至 2026-11-03
关联工单：[FBR-7713](https://chancetop.atlassian.net/browse/FBR-7713)
执行方式：Codex 内置浏览器真实页面操作；Mongo、Pod、镜像和文件元信息仅作只读佐证。

## 1. 部署基线

- Namespace：`uat`
- 站点：`https://social-media.connexup-uat.net`
- AM：`https://am.connexup-uat.net`
- API：`fbrdevacr.azurecr.io/uat/app/social-media-api:fbr-7713-20261102-4`
- Web：`fbrdevacr.azurecr.io/uat/app/social-media-web:fbr-7713-20261102-4`
- AM Console：`fbrdevacr.azurecr.io/uat/app/am-console-site:fbr-7713-20261102-7`
- `HTTPRoute/social-media`：`Accepted=True`、`ResolvedRefs=True`
- API/Web 各 1 Pod；API 单 Uvicorn worker，`Recreate` 发布。
- `/api/health`：`200`，`initialized=true`、`mongo=ok`、`storage=ok`、`storage_backend=am-media`。
- DNS `social-media.connexup-uat.net` 已存在并解析到 `20.1.217.183`；本轮未执行 Terraform apply。

证据目录：

`D:/java/DRI/social-media/storage/verification/20261102-2230-FBR7713/`

## 2. 真实商家与数据

| 项 | 实际值 |
|---|---|
| MA | UMETEA，`be4d56b7-6582-4185-8c8a-93b6861f3a95` |
| MA AM profile | canonical name `George Merchant`；source record `be4d56b7-6582-4185-8c8a-93b6861f3a95`；place_id `ChIJqVId_y1ZwokRjPCG1MQuKmg` |
| MB | choice brooklyn，`47c65660-ade7-4431-8d46-9ec9c58aef5b` |
| MB AM profile | canonical name `Choice Brooklyn`；3 个 location place_id 均按 AM 原始资料展示 |
| AM asset A | `Spooky Sips Halloween Boba Poster.png`，AM media ID `966a577f-0bc4-4c15-9c00-d18128cff942` |
| AM asset B | `Screenshot 2026-10-19 at 14.32.57.png`，AM media ID `532c52a7-a2bf-4590-b999-4fe2c22e36ac` |
| Brand A | `brand-153be69767854916877c5a00238ddf9e`，`UME_SMOKE_20261102-2230-FBR7713_A`，小红书 / 图文笔记封面 |
| Brand B | `brand-153be69767854916877c5a00238ddf9e-0d1fc263`，`UME_SMOKE_20261102-2230-FBR7713_B_IG`，Instagram / 竖版帖子 |
| MA brand rules | `_id=be4d56b7-6582-4185-8c8a-93b6861f3a95`，version `2` |
| 主整图任务 | `wtask_4efa6fe4dd87482a99fa70a297626e43` |
| 主整图 v1 | `wver_02633b7dea834fd8bde10868ad2baaac` |
| 主整图 v2 | `wver_5f3c138b854649569761f1c83ef720c6` |
| 独立改图批次 | `pbat_1b1d97db5d564a67986a556956ac5e72` |
| 独立改图任务 | `pedit_8d9a046a13f0445d80d79581872ffe66` |

## 3. 用例结果

| 用例 | 结果 | 实际过程与证据 |
|---|---|---|
| SM01 | 通过 | AM 选择 UMETEA 后点击 Open social media；新标签页只携带 merchant_id；页面显示 UMETEA，刷新及菜单切换后上下文保持。证据：`SM01-am-selected.png`、`SM01-ume-workspace.png`。 |
| SM02 | 通过 | 资产页显示真实 AM 素材；实际加载下一页；搜索 `Spooky Sips Halloween Boba Poster.png` 命中 1 项；不存在标记显示空态；清空恢复；在海报设计选择器确认该素材。证据：`SM02-search-hit.png`、`SM02-search-empty.png`、`SM02-am-asset-selected.png`。 |
| SM03 | 通过 | 通过真实文件选择上传 `WIDE.png`、`ALPHA.png`；后端日志依次为 social 201、AM SAS init 201、Azure PUT 201、AM complete 200；原图 hash 分别为 `d60574...fb65`、`9b73b7...b8b2`；原图尺寸分别为 2400×1600、512×512；未写入 AM Media Library。 |
| SM04 | 通过，缺资料分支未覆盖 | 创建 Brand A、上传 Logo/参考图、保存规则 version 2、刷新回读；复制出 Instagram Brand B 并刷新回读；两份风格均属于 UMETEA 且展示同一 AM profile。choice brooklyn 已有 profile，因此无法在指定第二商家验证“无 profile 待补充”分支。证据：`SM04-copy-created.png`、`SM04-copy-refreshed.png`。 |
| SM05 | 通过（修复后） | 真实选择 4 张 UME JPG，使用用户提示词完成一次提示词优化和一次生成；任务 `wtask_4efa6fe4dd87482a99fa70a297626e43`、v1 `wver_02633b7dea834fd8bde10868ad2baaac`；页面下载 PNG 为 1080×1440，hash `4d01f453...5c06`，Logo 和指定文案可见。修复前 1072×1440 证据保留为 `SM05-pre-fix-task.png`。 |
| SM06 | 通过 | 仅把“快乐”改为“欢乐”，接口与图片 Agent 两路均 `completed`；Agent run `07efe996-fc9c-4439-898a-3cfbc1446554`；v2 的接口/Agent 最终图均为 1080×1440；透明层不能作为继续改图来源；v1/v2 可切换，旧版选为编辑来源后实际下载 hash 等于 SM05；刷新后版本仍存在。证据：`SM06-gateway-done-agent-running.png`、`SM06-both-complete.png`、`SM06-refreshed.png`、`SM06-*-download*.png`。 |
| SM07 | 通过 | 独立“修改海报”上传 SM05 原图作为唯一底图；单候选任务 `pedit_8d9a046a13f0445d80d79581872ffe66`，run `b43e0fda-aa07-4519-98b1-756b038b0ca7`；透明图层与最终结果角色正确；最终下载 1080×1440，hash `db870f65...6a4a`，文案修改正确；任务中心按“修改海报”筛选可重新打开。 |
| SM08 | 通过 | 页面本地上传 `WIDE.png` 与 `BACKGROUND.png`，提交 `brep_bc4ea31f5d9540d3b3683326f0e85377`；持久化 run `fb148efa-38e7-4293-b260-584f4353976d`，截止时间与原始 run 一致；最终页面和下载均为 2400×1600。 |
| SM09 | 通过 | 任务中心“全部/海报设计/修改海报/已完成/生成中”筛选均实际改变列表；关闭 media 标签页后从 AM 选择 UMETEA 并找回同一 `wtask_4efa6fe4dd87482a99fa70a297626e43`；深链接刷新恢复 v2；图片预览和下载有效，下载 hash 与当前 v2 相同。 |
| SM10 | 通过 | UME 与 choice brooklyn 双标签页同时存在；MB 品牌列表为空、任务列表为空，素材选择器只有 MB 素材；UME 选择器仍为 UME 素材。MB 深链接携带 UME task ID 时页面明确显示“整图任务不存在”，未读出任务。证据：`SM10-choice-brooklyn-assets.png`、`SM10-choice-brooklyn-picker.png`、`SM10-ume-picker.png`、`SM10-cross-merchant-task-denied.png`。 |
| SM11 | 部分覆盖 | 实际 AM Sign out 后，MA media 页与任务深链接均显示 `AM Console 请求失败（HTTP 401）`，刷新仍拒绝；重新登录并选择 UME 后恢复；把 merchant_id 改为 MB 时只读取 MB 自己的范围，UME task ID 被拒绝。当前唯一提供的账号可访问 UME 和 choice brooklyn，缺少“只能访问 A、不能访问 B”的 AM_A 角色，因此非 owner 的 403 分支未覆盖。 |
| SM12 | 通过 | 提交 `brep_5e81b809e8764a84a3573e7e9ad4cd01` 后只读确认 `run_id=985b8ab5-28d9-4770-8a6a-e04fdfe1f120` 且状态 running；随后实际 Sign out 并关闭 media 页；等待后只读查询为 succeeded，run_id 保持不变，说明后台保存不依赖员工会话。 |
| SM13 | 通过（background replacement 恢复） | 提交 `brep_31061dc0e0eb40fab716207232a14617`，run `3817bc32-ae6a-4613-82a2-87e1004eff3f`；旧 Pod `social-media-api-5c97d794c-8c8qc` 终止后新 Pod `social-media-api-5c97d794c-jsl2v` 于 `16:48:54Z` readiness；Mongo 在新 Pod 启动后仍记录同一 run 处于 running，最终以同一 run 成功，未重新 trigger；页面下载为 2400×1600。证据：`SM13-running-before-restart.png`、`SM13-resumed-after-restart.png`、`SM13-resumed-download.png`。 |
| SM14 | 通过 | CLI enable 后状态 `enabled=true`；页面上传、提示词优化、生成设计、品牌保存均显示维护拒绝；历史任务读取和下载仍成功；`wait` 返回 drained；维护中重启 API 后状态仍 `enabled=true`；disable 后实际上传 `SOURCE.png` 成功。证据：`SM14-*.png`。 |
| SM15 | 通过（修复后） | 空海报设计生成按钮禁用；空修改提交提示“请先添加 1–5 张参考图片”；`INVALID.png` 提示“无法识别的图片文件”；修复前 60,062,632-byte `OVER_LIMIT.png` 会持续加载，Web 增加 20 MB 客户端预校验后复测立即提示“文件超过 20 MB 限制”；随后选 `SOURCE.png` 成功显示 1/5。证据：`SM15-*.png`。 |

## 4. 本轮修复

1. 整图生成输出按订单画布尺寸归一化，避免网关 1072×1440 原样落库。
2. 增加“复制为独立风格”页面入口，支持选择新平台/用途并刷新回读。
3. 资产页合并商家临时上传与 AM Media Library，刷新后临时素材仍可选取。
4. 上传 API 增加 20 MB 客户端预校验，避免超限文件长期停在“上传中”。
5. 增加 `brand_rules.merchant_id` 索引；真实 Mongo `notablescan` 查询已验证相关集合均走索引，见 `index-plan-summary.json`。

## 5. 自动化与运维验证

- Social backend：`285` tests，`0` failures，`0` errors，`6` skipped；JUnit XML 见 `pytest-results.xml`。
- Social frontend：`npm run build` 通过；`npm run lint` 通过。
- AM Console：`./gradlew.bat :frontend:am-console-site:check` 通过。
- 真实 Mongo 只读数据：`mongo-summary.json`。
- Mongo 索引计划：`index-plan-summary.json`，覆盖 assets、brands、brand_rules、whole_tasks、whole_versions、poster_edit_tasks、background_replace_tasks、media_objects。
- 维护状态跨 API Pod 重启保留；关闭维护后写操作恢复。

## 6. 遗留与边界

- SM04 的“AM 无 profile”分支未覆盖：指定商家 choice brooklyn 实际已有 profile，未删除或伪造资料。
- SM11 的非 owner 403 分支未覆盖：现有账号具备 UME 与 choice brooklyn 的访问权限，缺少用户要求的独立 AM_A 仅单商家账号。未把管理员可访问两家误报为越权失败。
- 整图生成的任务恢复范围保持既定边界；本轮验证的是 background replacement 的同一 run 恢复。
- 未访问或测试 SEE JOY。
- 未执行 Terraform DNS apply；现有 DNS 已完成只读验证。
