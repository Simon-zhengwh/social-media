# Core AI 沙箱运行与产物交付

## 加载与输入

先 `use_skill(name=<本会话真实绑定的qualified name>)`，从返回的base_dir使用本包scripts/assets/references。资源可用实际存在的read_skill_resource按需读取。不要猜base_dir、qualified name、Python路径或容器目录。

附件只认本轮平台消息里的真实sandbox_path/media_id/URL；URL输入可下载到本次沙箱供Pillow/OpenCV使用，保留原字节及来源。不能读取用户电脑D盘或其他会话的附件。Python/Pillow、OpenCV/NumPy和浏览器依赖在当前沙箱检查，缺失时按需求安装requirements，禁止为了生图安装SDK或读取业务密钥。

当提示词里有多个URL时，先区分海报与每张替换素材，不把所有图都当作编辑目标。模型生成步骤由Agent原生generate_image完成；run_python_script/run_bash_command等代码工具只处理文件、CPU分割、配准、构建和验收。

## 工具回执与原生图片

每次生成前保存实际prompt、输入引用与请求参数，调用后把**实际原样**返回文本存为tool-result.txt，保留链接和media_id。原生结果已经是沙箱文件就直接使用；只返回可访问HTTP(S)链接时可用下面的--download读取文件。普通下载不调用生图网关，不携带业务API Key。

```bash
python scripts/record_core_ai_generation.py --tool-result work/background-tool-result.txt --image work/native-background.png --download --model gpt-image-2.5-sunburst --background opaque --purpose dish-background --prompt-file work/background-prompt.txt --reference work/poster.png --out work/background-generation.json --review-out work/background-review-pending.json
```

已有原生文件时省略--download。--reference为实际入模引用对应的原生文件，可重复；改字第二阶段必须同时记录原海报和已验新贴纸。不要将未入模的替换原照加成假引用。

purpose按素材职责选择：dish-background、photo-template、background-foreground、text-sticker、text-final、element、fixed-layer、opaque-background。背景参数是实际请求值，未传时写auto；工具只支持自动不透明时仍须在prompt中要求opaque，并验证结果。透明用途必须实际请求transparent。

所有新生图及记录器使用gpt-image-2.5-sunburst和xhigh，记录器会拒绝其他model或quality的新结果。调用前核对工具支持这两项，不支持时报告能力缺失。已实际发出的低quality请求须保留真实原回执及失败/弃用说明，不能把回执quality改成xhigh。复用已有历史图片时保留原始quality并明确本轮没有重新生成。

记录器校验单条真实成功工具回复、原生PNG和所需透明度；写与当前构建器兼容的tool/purpose/prompt/sha256/references，并保留原样tool_result、media_id、model、quality和实际链接。它不能证明文案/位置正确，也不能证明模型调用真的发生；Agent不得用本地候选和编造文本生成假回执。拿不到原生文件或回执格式不符就停止后续导入，不把缩略图当原生图。

--review-out只创建 `status: PENDING, image_sha256, notes`，实际视觉检查后才能填写PASS/FAIL。菜品背景的正式审核须另用build_dish_editor.py --prepare-background生成带最终合成像素哈希的记录，不能用此通用PENDING文件替代；菜品抠图审核继续用cutout_sha256。

失败、未知、成功但弃用的尝试分别保存原错误/回执与原因，并列入manifest的generation.reports；每次请求都计入new_calls。成功收据不改写成网络失败；弃用原因另记。未知调用不自动再生成。上游复用和本轮请求分开，不能把构建零调用称为整单零调用。

## 视觉与浏览器

优先当前Agent已有视觉能力。若不能看图，只在平台确实暴露且适合时用caption_image等视觉工具；不编造工具名，不把同一图片反复注入上下文。长任务用缩略图/局部图，只传回必要文字结论，遵守更严格的宿主规则。

用包内check_editor.py验证实际HTML，不逐单临时编写浏览器脚本。Playwright或浏览器不可用时标为not_performed/pending，并说明具体缺失，不能冒充浏览器通过；技术/视觉/浏览器独立记录。没有独立视觉检查时不将技术结果写为视觉PASS。

## 发布

使用本会话实际已有的submit_artifacts或等价产物工具，按其真实schema提交：

- HTML类：editor.html为主，附完整结果目录或离线ZIP、manifest、原生模型PNG/回执、完整原照、mask、审核及report。单独HTML须已内嵌全部素材，不依赖CDN。
- 改字：final.png为主，附sticker.png、原稿、两阶段回执/审核、edit-region、manifest和report。

只用发布工具返回的真实download_url，不把远端沙箱路径或本机file://路径当作下载链接，不擅自改公开权限，不用静态PNG替代HTML主交付。保留模型图片原始Markdown链接和media_id；不能发布文件时如实报告，不编造产物链接。

可复跑目录保留相对路径，不为缩小HTML把原照转JPEG。需在沙箱生成交付ZIP时可用Python标准库shutil.make_archive，只打包本次结果目录，不含密钥或整个工作区。不要调用旧版editor_workflow.py的local/local-artwork命令，本版只有SKILL.md列出的当前构建脚本。
