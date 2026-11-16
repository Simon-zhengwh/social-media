# 批量执行与素材复用

## 一次准备，按证据决策

每个阶段用一次代码工具调用执行已有脚本并读取简短结果。先运行，只有具体错误才读对应实现；不要在多个工具回合分别查看文件大小、哈希、像素和同一批报告。HTML含内嵌图片，不直接cat或grep整份文件；需要检查时解析editor-data，只输出指定字段，不输出data URL/base64。

先检查一次真实视觉入口：原生图片输入或当前配置的视觉工具返回了实际图像/有效描述，才可用于视觉判断。若read_file只返回“Provide a detailed and vivid description...”等模板且没有实际图像/描述，不继续逐图重复调用；从当前工具目录发现可用入口，不猜工具名或私自换Agent/模型。没有可用入口时保留PENDING，说明缺少视觉证据；尺寸、哈希、OCR和浏览器通过不能替代语义验收。

## 菜品准备

一次下载原海报和全部完整原照，记录来源。初始修补范围用覆盖全部旧菜、容器、纸垫和投影的简单polygon/mask；protected_regions只标确实压在该范围上的文字或装饰。先看整体范围，不在首轮生成前写多套自动追踪旧盘轮廓的脚本；出现可定位的覆盖失败后才精修对应边界。输入本来截断的盘沿记录为素材限制，不反复抠图或生图补齐。

manifest保留原有字段，每张新菜增加内联segmentation配置，字段与segment_photo.py相同但省略image，来源固定为original。例如一张原照为1200×900、海报为1536×1152时：

```json
{
  "mode":"dishes",
  "poster":"poster.png",
  "repair_region":{"polygon":[[420,250],[1480,250],[1480,1040],[420,1040]]},
  "dishes":[{
    "id":"dish-one", "original":"dish.jpg", "box":[450,280,1450,1040],
    "segmentation":{"box":[0,30,1200,880],"foreground":[[600,450]],"background":[[20,20]],"max_size":1254}
  }]
}
```

数值仅示例，按实际图片填写。segmentation.box/种子在EXIF校正后的原照坐标中；外层box是海报中的默认摆放范围，不能混用。支持seed_mask、corrected_mask和已有真实alpha。多张原照一起放入dishes。

```bash
python scripts/build_dish_editor.py --manifest work/dishes.json --prepare-assets --out work/prepared-v1
```

一次输出：

- `layout/input-report.json`和布局分页图：全部尺寸/哈希、修补/保护范围、可选removal_scope覆盖检查。
- 每张菜品的cutout.png、mask.png、分割report及**PENDING** cutout-review.json；原照RGB和完整尺寸保持。
- `preparation.json`：简短runtime状态、全部对照页和dish_updates。将每项的cutout、segmentation_report、cutout_review一次写回对应id的原manifest，保留原有original、box与其他字段。脚本不修改输入manifest，不生成成品、不写视觉PASS。

已有cutout时需同时提供原segmentation_report和cutout_review；脚本核对原照哈希、完整RGB和PASS绑定后复用，过期或未审核则报错。需要重分割的菜品移除该项旧cutout路径，填segmentation；其他合格项保持不变。原来的--prepare-layout和segment_photo.py命令继续可用。

runtime提前检查cv2、numpy、playwright和实际chromium路径。缺少本分支依赖时在生图前一次安装所需requirements；需要浏览器且缺失时一次执行`python -m playwright install chromium`。已有chromium路径可传给check_editor.py的--browser，避免检查失败后才下载。脚本仅检查，不自动安装或启动用户浏览器。环境不允许安装时如实报告检查未执行。

对照页每页最多960×900，先集中看布局和全部抠图，再按明确缺陷看局部。修补范围须包含完整旧菜、盘沿及投影；保护区不包含旧照片残片。removal_scope若有独立标注，脚本要求完全覆盖，不能为通过而缩小它。数值通过后仍需真实视觉确认。

## 背景返回后

范围确认后默认用明确prompt请求完整不透明背景；工具mask只在确需局部控制、网格与语义已经明确时使用。不为猜工具缩放坐标持续生图。

一次代码调用保存原样回执、运行record_core_ai_generation.py和下面命令、读取简短结果：

```bash
python scripts/build_dish_editor.py --manifest work/dishes.json --prepare-background --out work/scene-review-v1
```

集中看原稿、模型背景、最终空背景、范围叠图和四边界对照。审核绑定最终scene_rgba_sha256；修改背景、修补区或保护区后重新准备和审核该场景，其他来源未变的合格抠图继续复用。

| 可定位问题 | 下一步 |
|---|---|
| 模型背景已修好，最终合成仍有旧物 | 改repair_region/protected_regions，复用背景重新prepare-background |
| 模型背景仍有旧物，工具mask挡住该处 | 一次纠正完整范围，再针对缺陷生成 |
| 模型背景纹理、阴影或接缝不合格 | 保存缺陷位置、前后对照和拟修改项，再生成针对性修正 |
| 菜品漏抠、灰边或杂点 | 改该菜品种子/corrected_mask，复用背景及其他菜品 |
| 只改摆放、旋转或原照背景开关 | 用HTML调整或本地重建 |
| 原照本来裁断容器 | 记录素材限制，按现有像素摆放，不重绘缺失部分 |

对同一缺陷连续两轮修正没有新增定位证据或可验证改善时，停止该分支并保留FAIL/PENDING及证据。新证据明确下一步才继续；不能靠增加无依据尝试、用新菜遮住残留或降低验收范围收尾。原本未受改动的装饰不做额外美化；修补破坏了应保留设计则必须修复或报告失败。

## 构建与交付

背景与全部抠图真实审核通过后，在一次代码工具调用中顺序构建HTML、用已确认浏览器运行check_editor.py、打包完整目录。执行失败就停在该阶段，不提交“完成”产物；浏览器结果与视觉结论分开。需要查看默认摆放成图时一次导出并与原稿对照，不通过修改构建后的HTML绕过manifest与哈希记录。

原生生图固定gpt-image-2.5-sunburst、xhigh；每次调用保留原样回执，失败/弃用原因单独记录。所有重试计入本轮调用；批处理代码从不生图、调RGB或自动批准审核。
