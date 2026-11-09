#海报修改独立菜单 —设计

日期：2026-11-07
状态：待实现

##背景

git历史（`ad2c9ee^`）里有已下线的「海报修改」功能：
-后端 `backend/app/routes/poster_edit.py` + `services/poster_edit.py`
-前端 `frontend/src/components/PosterEditStudio.tsx`
-数据集合 `poster_edit_tasks`

它在提交 `1461a66`被整体移除，理由是「只保留海报设计（整图）工作流」。移除时它的 agent调用是：

```python
agent_image.run_agent_images(
 client,
 agent_id=settings.core_ai_image_agent_id, # ←同一个 agent
 input_text=agent_input,
 timeout_seconds=settings.core_ai_image_agent_timeout_seconds,
)
```

今天原照替换用的正是 `settings.core_ai_background_replace_agent_id`，且我们刚把它和 `core_ai_image_agent_id`都指向 `5a162c34-...`。**两者确实调用同一个 agent。**

##目标

新增左侧导航菜单项「海报修改」，独立页面：
-用户上传1–5张参考图（第一张视为待修改的海报）
-输入一段自然语言修改要求
-可指定候选数量1–6
-后台调用 image-editor agent，返回透明层与最终图
-结果可预览、切换候选、下载

##与「原照替换」的区别

| |海报修改 |原照替换 |
|---|---|---|
|入口 |左侧导航独立菜单 |海报设计工作台内页签 |
|依赖 |不依赖某个已有海报任务 |必须先有一张成功的海报 |
|输入 |1–5张参考图 +文字 |当前海报 +1–5个「目标→原照」映射 |
|输出 |多候选（1–6）透明层 +最终图 |单个替换结果，对比后应用为新版本 |
|数据 |新集合 `poster_edit_tasks` |`background_replace_tasks` |

两者共用 agent，但流程与数据独立。

##后端

###新文件：`backend/app/services/poster_edit.py`

从 `ad2c9ee^`恢复，但必须改造：

1. **产物挑选改用新逻辑。**旧代码调用 `agent_image.run_agent_images` / `classify_agent_images`，这两个函数在本次重构里已删除。改为：
 - `client.run_agent_detail(agent_id, input_text, timeout_seconds=...)`
 - `agent_image.pick_final_image_urls(result)`取最终图候选
 - `agent_image.pick_transparent_image_url(result, excluded=最终图 URL)`取透明层
 -下载后 `agent_image.has_transparency`校验
 - `agent_image.save_png_asset(...)`保存

2. **尺寸约束。**旧代码以第一张参考图的宽高作为 `target_size`。保留，但改为「可选」：若第一张图能解码出尺寸就用于校验与归一化，否则不强制。

3. **重试与恢复。**保留旧的 `run_id`持久化与 `resume_tasks`，使服务重启后能继续查询同一 run。

4. **错误码不变**：`reference_required` / `reference_missing` / `reference_invalid` / `prompt_required` / `candidate_count_invalid`。

###新文件：`backend/app/routes/poster_edit.py`

从 `ad2c9ee^`原样恢复，接口不变：
- `POST /api/poster-edits`（单任务）
- `POST /api/poster-edits/batches`（多候选）
- `GET /api/poster-edits/batches/{batch_id}`
- `GET /api/poster-edits/{task_id}`
- `GET /api/poster-edits/{task_id}/download?output_id=...`

###注册
- `backend/app/main.py`：`import`并 `include_router(poster_edit.router)`，启动时调用 `poster_edit.resume_tasks(...)`
- `backend/app/schemas.py`：恢复 `PosterEditRequest`与 `PosterEditBatchRequest`

###Agent配置
复用 `settings.core_ai_image_agent_id`（当前 `5a162c34-...`）。不新增环境变量。

##前端

###新文件：`frontend/src/components/PosterEditStudio.tsx`

从 `ad2c9ee^`恢复并适配当前代码：

1. **产物类型。**旧文件依赖 `PosterEditOutput`。当前 `types.ts`中已无此类型——重新添加：
```ts
export type PosterEditOutput = {
 id: string
 channel: 'gateway' | 'agent'
 role: string
 label: string
 asset_id: string
 width: number
 height: number
 selectable: boolean
 preview_url: string | null
 download_url: string | null
}
export type PosterEditTask = {
 id: string
 batch_id: string | null
 candidate_index: number
 candidate_count: number
 status: 'queued' | 'running' | 'succeeded' | 'partial' | 'failed' | 'interrupted'
 stage: string
 progress: number
 prompt: string
 references: Array<{ asset_id: string; name: string; preview_url: string; width: number; height: number }>
 outputs: PosterEditOutput[]
 error: { code: string; message: string } | null
 created_at: string
 updated_at: string
}
export type PosterEditBatch = { id: string; tasks: PosterEditTask[] }
```

2. **API客户端。**`frontend/src/api.ts`恢复四个函数：`createPosterEditBatch` / `getPosterEditBatch` / `getPosterEdit`。

3. **URL参数。**用 `?view=poster_edit` + `?poster_batch=<id>` + `?candidate=<n>`。路由切换沿用 `App.tsx`现有的 `view`参数机制。

###左侧导航
`frontend/src/components/Sidebar.tsx`新增一项：
- key: `poster_edit`
-标题：「海报修改」
-副标题：「参考图与文字修改」
-位置：紧跟「海报设计」之后

###路由
`frontend/src/App.tsx`：
- `view === 'poster_edit'`时渲染 `<PosterEditStudio>`
-未指定 `view`时保持现有默认（海报设计）

##数据

复用旧集合 `poster_edit_tasks`。历史文档字段与新代码兼容（`input.prompt`、`input.asset_ids`、`outputs`、`run_id`、`agent_input`），无需迁移。

##不做的事

-不改原照替换的任何逻辑
-不改海报设计工作台
-不新增 agent配置项
-不做「从已有海报选底图」的入口（本轮只做参考图上传）

##验证

1.后端：`pytest tests/test_poster_edit.py -q`（需从 `ad2c9ee^`恢复测试并适配新挑选逻辑）
2. `npm run build` + `npm run lint`
3.浏览器实测：
 -左侧出现「海报修改」
 -上传2张图 +写要求 →提交 →轮询 →出现结果
 -候选切换、下载可用
 -刷新页面按 URL恢复
4.全量后端测试不新增失败（基线6个环境失败）
