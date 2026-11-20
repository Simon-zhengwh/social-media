# 系列图（Poster Series）启用、接口与回滚

系列图是 **默认关闭** 的 opt-in 功能。关闭时单张海报的页面、接口、后台任务和列表行为保持不变。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `POSTER_SERIES_ENABLED` | `false` | 总开关。关闭时系列路由返回 404（`/series-config`、`/workspaces/{id}/mode` 除外），列表不返回系列任务，也不会创建系列索引或恢复系列任务。 |
| `POSTER_SERIES_ASSET_UPLOAD_ATTEMPTS` | `3` | 单张素材上传到 Core AI 的最大尝试次数（仅网络类瞬时错误重试）。 |
| `POSTER_SERIES_ASSET_UPLOAD_RETRY_SECONDS` | `1` | 重试间隔基数，按次数线性递增；设为 `0` 关闭等待。 |
| `CORE_AI_SERIES_AGENT_ID` | 空 | 已发布的系列图 Agent。为空时系列页失败并给出配置错误，**不会**回退到其他模型或直接网关。 |
| `CORE_AI_SERIES_AGENT_TIMEOUT_SECONDS` | `1800` | 本轮 Agent 等待时间。 |

## 失败与重试

- **素材上传**：`_agent_asset_urls` 只对连接重置、超时、DNS 等可判断的网络瞬时错误重试，最多 `POSTER_SERIES_ASSET_UPLOAD_ATTEMPTS` 次；素材不存在、未配置、空文件等永久错误立即失败。每成功上传一张就写入 `whole_series_groups.agent_asset_urls`，因此页重试只补传失败的素材，不重跑整个素材池。
- **生图触发不重试**：触发请求结果未知时只核对已保存的 `run_id`，不会为了重试而重复触发生成。
- **Logo 不接受替换**：系列页的 Logo 始终由后台用本组官方原文件在固定显示框叠加。原照替换请求只要目标名包含 Logo/标识/标志/商标，或引用了本组/品牌的官方 Logo 素材，创建时返回 422 `series_logo_replacement_not_allowed`，提示去掉 Logo 后重新生成仅替换照片的原照替换。旧记录（已有显式 Logo 项）在应用（apply）和 HTML「保存到本页」时同样被拦截，模板是第二个可见 Logo；重新生成照片记录后即可保存。单张海报的显式 Logo 替换行为不变。

## 演示启用（UAT/本地）

1. 在 `backend/.env` 中设置：

   ```dotenv
   POSTER_SERIES_ENABLED=true
   CORE_AI_SERIES_AGENT_ID=a91e2488-0285-40e3-bff6-540a83e9d944
   ```

   `a91e2488-0285-40e3-bff6-540a83e9d944` 是 UAT 的 `simon-image-generate`，绑定 `simon/poster-series` Skill。新版页面生成需要 Agent 和 Skill 支持下方的 `poster-series-generate`。

2. 重启后端，确认开关：

   ```powershell
   Invoke-RestMethod http://127.0.0.1:8000/api/whole-image/series-config
   # {"enabled": true}
   ```

3. 打开海报设计页，切换到「系列图」，选模型和组数后点击「生成」。每轮只启动一条 Agent Run。Agent 按用户需求决定每组张数和内容，应用按返回顺序展示图片。首次提交后模式锁定。「历史记录」按轮次显示结果，下载时先选中具体图片和版本。

## Core AI 整组交付

- 输入操作是 `poster-series-generate`，`prompt` 是用户需求，`groups` 指定组 ID 和模型。素材、品牌资料、画布及 Logo 信息以独立字段传入。
- Agent 自己决定每组张数、内容和用图，并维护整组视觉风格。需要 Logo 时，Agent 为整组设计同一个位置、尺寸和与画面融合的局部背景。应用按 `logo_box` 合成官方 Logo 文件。
- Agent 提交各张 PNG，再返回 `groups` 数组。组内每张图片提供对应的制品 URL 及实际引用的原照。历史同协议回执也可使用能唯一匹配制品的文件名。数组顺序就是展示顺序。

```json
{
  "groups": [
    {
      "id": "wsgrp_...",
      "model": "gpt-image-2.5-sunburst",
      "logo_box": { "x": 900, "y": 60, "width": 82, "height": 82 },
      "images": [
        { "image_url": "https://example.com/group-1-image-1.png", "content_refs": [{ "asset_id": "asset_...", "target": "甜品" }] }
      ]
    }
  ]
}
```

## 回滚

1. 在 `backend/.env` 设 `POSTER_SERIES_ENABLED=false` 并重启后端，或直接移除该行。
2. 可选：清空 `CORE_AI_SERIES_AGENT_ID`。已存在的系列任务不会出现在单张列表，单张历史任务/版本/候选/定稿不受影响。
3. 不需要回滚数据库。系列数据位于 `whole_workspace_modes`、`whole_series_runs`、`whole_series_groups`、`whole_series_pages`、`whole_series_html_saves`；关闭开关后不再读写这些集合，也不需要删除。若需彻底清理，按 `merchant_id` + `workspace_id` 删除这五个集合中的对应记录即可。

## 接口（均在 `/api/merchants/{merchant_id}/whole-image` 下）

| 方法与路径 | 说明 |
| --- | --- |
| `GET /series-config` | `{"enabled": bool}`；关闭时也返回。 |
| `GET /workspaces/{workspace_id}/mode` | 工作区模式；缺省时按已存任务推断，历史工作区为 `single`。 |
| `POST /workspaces/{workspace_id}/series-runs` | 一轮系列提交。`submit_id` 幂等；`ratio` 省略时继承所选平台/用途画布，不再强制 9:16。 |
| `GET /workspaces/{workspace_id}/series-groups` | 恢复全部轮次/组/页；每组带 `content_pool`（上传的内容素材，已排除风格参考与 Logo）和 `style_reference_ids`。 |
| `POST /series-groups/{group_id}/interiors` | 旧流程记录的兼容入口。 |
| `POST /series-groups/{group_id}/pages/{page_id}/retry` | 复用该页已有任务 ID 重试/续查，不新建页。 |
| `POST /series-groups/{group_id}/pages/{page_id}/html-saves` | 隔离 HTML 编辑器「保存到本页」。见下方契约。 |
| `GET /series-groups/{group_id}/pages/{page_id}/download` | 当前页所选版本的 PNG。 |
| `GET /series-groups/{group_id}/download` | 兼容整组 ZIP 接口。工作台提供单张下载。 |

### HTML 保存契约

```json
{
  "source_version_id": "wver_...",
  "expected_output_id": "series_final",
  "artifact_id": "wart_...",
  "session_id": "session-...",
  "png_base64": "<PNG>",
  "width": 1080,
  "height": 1350,
  "content_refs": [{"asset_id": "asset-...", "target": "莓果杯"}]
}
```

- `artifact_id`、`expected_output_id` 必填；artifact 必须是本页某个原照替换任务产出的 HTML（`kind=html`），且替换记录的来源版本/结果与请求一致。
- `content_refs` 是前端当前页默认值（通常是旧图）。后端不直接采信，而是从来源版本关联出发，套用已验证替换记录的「目标位置 → 新素材」映射，并用 HTML 回传只确认替换记录实际引入的素材。旧图不会复活，`@图2 → @图4` 会保存为 `@图4`。
- 保存时持有页锁并用 `current_version_id + current_output_id` 做 CAS；并发改图、另一编辑器会话或过期会话返回 409，不覆盖新版本。同一 `session_id + 来源 + PNG 哈希 + 关联` 重复提交只产生一个版本。
- PNG 必须可解码且与组画布尺寸一致（422）；保存后自动按组固定 Logo 框叠加官方 Logo，无 Logo 底图保留供再次编辑。
- 返回 `page_out`。

## 日志与验证

- `model_input_latest.agent` / `series.agent_trace_verification` 记录实际 `run_id`、`generate_image` 调用或原生长工具 span、验证级别：
  - `args_verified`：运行记录公开了工具参数，严格校验模型、全部素材 URL 和提示词。
  - `tool_span_reported`：运行记录的工具参数为空，但 trace 中存在成功的原生 `generate_image` span；模型/素材角色/封面引用由 Agent 结构化回执校验，PNG 由后端解码校验，并记录 caveat。
  - 两者都没有时页面失败（不会仅凭完成状态放行）。
- 后端回归：`backend` 下 `.\.venv\Scripts\python.exe -m pytest`。
- 前端（由前端工作分支执行）：`npm test`、`npm run lint`、`npm run build`。

## UAT 证据

本分支的实施记录（不提交）：`.scratch/uat/UAT-01-02-report.md`、`.scratch/uat/UAT-01-02-evidence.json`。四个模型各有一份真实封面运行，另有一份 gpt 介绍页运行带锁定封面引用；全部返回 1080×1350 的 `page.png`。系列 skill 版本 0.1.1（digest `ef7d24a5f7c01ac9fb59b0be76907a6de4494ad0271913989feba77ebeeebb41`），UAT Agent `a91e2488-0285-40e3-bff6-540a83e9d944` 已绑定 `simon/poster-series`。

发布态复核（只读 GET，2026-11-18T17:38Z 抓取，见 `.scratch/uat/release-evidence.json`）：

- `GET /api/skills/6afdc32329bcbab6c6115286` → `version=0.1.1`、`digest=ef7d24a5...`、`updated_at=2026-11-18T16:41:02Z`。
- `GET /api/skills/6afdc32329bcbab6c6115286/download` → 返回的 `content` 与仓库内 `core-ai-skill/poster-series/SKILL.md` **逐字节相同**（sha256 `54eb13d5af028de38aab1256cd25f0519d9ddad971b909d32e5925e5b5e9fd5d`）；`eval/eval.json` 仅行尾（CRLF）差异，规范化后相同。
- `GET /api/agents/a91e2488-...` → `status=PUBLISHED`、`definition_revision=2`、`skills=[poster-series]`、工具为四个 builtin（media-generation / code-execution / file-operations / multimodal）。
- 时间线：Agent 配置 15:24:39Z 发布；skill 0.1.1 于 16:41:02Z 上线；报告内的 5 次真实运行（gpt 封面 16:41:28Z、seedream 5 pro 16:43Z、seedream 5.5 16:52Z、minimax 16:54Z、gpt 介绍页 17:01Z）均在其后。更早的 `1fe4113b`（16:21Z 失败）与计划探测（15:28Z）属于 0.1.0，只作历史参考，不作为发布态证据。
