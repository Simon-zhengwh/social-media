# 菜品模式：完整背景 + 原照/抠图双版本 + 自由叠放

## HTML中自由选择原照背景

每个菜品同时提供两种图片，来自同一张原照，不是两次模型重绘：

- **去背景（默认）**：OpenCV生成的透明菜品及自带容器，仅新增alpha、不改RGB，显示海报已修补的环境背景。
- **保留原照背景**：显示完整原照，原照中的灰底、桌面等也随该图层移动和导出。不是恢复旧海报的菜品，也不是替换整张海报背景。

两张图尺寸和像素坐标一致。HTML每个菜品独立选择，切换不重算取景、不改变位置/缩放/旋转/显隐，不裁回旧菜轮廓；复制菜品继承当前选择，之后分别切换。整张PNG和单独菜品PNG都按当前选择导出；单独PNG在保留背景时可能完全不透明。

原照与抠图均内嵌，离线切换不调用模型或OpenCV，不额外生图。普通完整原照仍需在制作阶段生成并验收cutout。后续在HTML中单独添加的透明PNG若没有对应原照，只提供去背景版本并禁用保留背景选项，不伪造已丢失的背景。刷新回默认去背景；位置重置保留当前背景选择。

## 生成前先校准范围

按[批量与复用流程](efficient-workflow.md)运行build_dish_editor.py --prepare-assets，一次检查布局、环境依赖并分割全部新菜；新菜分割配置内联在dishes[].segmentation，原照来源固定为original。仅检查范围时继续用--prepare-layout。此阶段不需要background或视觉PASS。脚本输出preparation.json与PENDING审核，真实视觉检查后再更新原manifest，不擅自批准。

## 补背景与配准

原海报是唯一编辑目标。去掉全部目标旧菜品、自带容器和投影，局部补成原本环境；不插入新菜品，不拆文字、Logo或周围香料。使用gpt-image-2.5-sunburst、quality=xhigh，输出opaque。默认用prompt说明完整范围，工具mask仅在确有需要且网格已明确时使用。

```text
Use case: precise-object-edit. The supplied completed poster is the ONLY edit target.
Remove ONLY [each old dish AND its entire container AND cast shadow, explicit original-canvas locations]. Fill their former occupied regions with uninterrupted matching original [wood/table/scene], coherent grain, perspective, light and texture. No old food, plate rims, shadows or ghosts may remain anywhere along the complete boundary.
Keep all unselected regions, logo, words, stickers and decoration in original positions. Do not draw replacement food; real cutouts will be overlaid later.
Preserve original full-frame aspect and composition. Output one completely OPAQUE PNG, all pixels filled, no alpha holes, checkerboard or blank margins.
```

只在repair_region采纳补背景，区域外取原海报。若模型输出画幅改变，先修正或在未改区域配准，不直接拉伸。配准exclude-mask白色排除旧菜品/修补区域，黑色为可匹配的稳定背景、文字等：

```bash
python scripts/register_background.py --poster poster.png --generated model-background.png --exclude-mask repair-mask.png --out registered-v1
```

保存矩阵、内点数、误差、修补区覆盖率。内点不足、误差过大或修补区未完全覆盖均拒绝；数值通过后仍需看补背景是否无旧盘沿且衔接自然。

## 原照CPU抠图

```json
{"image":"original-photo.jpg","box":[10,20,1200,1190],"foreground":[[600,600],[300,900]],"background":[[20,20],[1180,1100]],"iterations":5,"max_size":1254,"min_component_area":16,"fill_holes":false}
```

按真实图片标注box和种子，box包完整菜品/容器；foreground覆盖食物与盘沿，background覆盖灰底等原照场景。fill_holes仅在容器实心区域要完整保留时启用，不能填真实孔洞。GrabCut删除未由显式前景种子保护的极小孤立噪点；较大碎片、细边和截断仍需检查。

输出cutout.png保持完整原照尺寸和全部RGB，仅更换alpha；mask.png是实际分割，report.json记录CPU方法、来源哈希、接触边界和清掉的小分量。支持corrected_mask导入校正。接触输入边界要说明原始内容缺失，不生成补齐；不凭运行成功写视觉通过。

## 菜品manifest

```json
{
  "mode":"dishes",
  "title":"菜品自由调整",
  "poster":"poster.png",
  "background":"registered-v1/aligned-background.png",
  "repair_region":{"mask":"repair-mask.png"},
  "removal_scope":{"mask":"independent-old-photo-scope.png"},
  "background_review":"background-review.json",
  "protected_regions":[{"id":"price","mask":"price-mask.png"}],
  "dishes":[
    {"id":"dish-one","label":"卤鸡与餐盘","original":"original-photo.jpg","cutout":"cutout-v1/cutout.png","segmentation_report":"cutout-v1/report.json","cutout_review":"cutout-review.json","box":[170,530,990,1215]}
  ],
  "generation":{"new_calls":1,"upstream_calls":0,"reports":["background-generation.json"]}
}
```

路径相对manifest。dishes支持1到多个，每张各有原照、RGBA及校验，不重复生成整张海报。

无需新增manifest字段：`original`为保留背景版本，`cutout`为默认去背景版本。构建器将两份完整字节一起内嵌，不能只把original留在旁边文件而不放进独立HTML。抠图与原照必须同尺寸，才能无跳动切换。

- background为与原稿完全同尺寸且完全不透明的背景。原生成结果不同则先修正或显式配准；构建器不自行contain/拉伸。
- removal_scope为可选的独立完整摄影移除范围，格式同repair_region。它须被扣除保护区后的有效repair_region完整覆盖，支持生成前和最终构建检查。几何覆盖通过后仍需核对原图语义范围。
- repair_region只用于旧菜品补背景范围，不裁新菜品。覆盖完整旧食物/容器/投影，可包含必要过渡余量；不能用几个采样点代替完整盘沿检查。
- protected_regions恢复跨修补区的原始设计并置于新菜品上方。按真实图形轮廓标注，不用包围框带回旧菜品碎片。其他区域原稿直接保留。
- box只决定cutout可见bbox的初始等比摆放，不裁切RGBA。用户之后自由调整。
- 普通JPEG不能直接当dish cutout；HTML添加功能只接收已有可见图案和透明背景的PNG。

## review记录与拦截

先输出最终局部合成后的空背景。该步骤只需poster、background、repair_region和可选protected_regions，允许先审核背景再制作抠图。

```bash
python scripts/build_dish_editor.py --manifest dishes.json --prepare-background --out scene-review-v1
```

prepare-background同时输出background-review分页图，集中对照原稿、模型背景、最终空背景、范围及四边局部。实际检查scene-review-v1/clean-background.png的完整旧菜品边界和保护区。生成的background-review.json初始为PENDING，自动填写输入文件SHA256与最终RGBA像素哈希。审核通过后补充结论、改为PASS，并将manifest的background_review指向该文件。

```json
{"status":"PASS","background_sha256":"背景输入PNG的真实SHA256","scene_rgba_sha256":"prepare步骤填写的最终合成像素哈希","notes":"实际视觉检查结论，含完整旧盘沿、投影、保护区与衔接"}
```

每张cutout另记status、cutout_sha256和notes，检查灰边、杂点、细边、孔洞和截边限制。背景输入或最终合成像素变化后重新预览审核，尤其是repair_region、protected_regions或原海报发生变化时。保留真实PENDING/FAIL状态，修正并重新验收后才能记录PASS。

构建器核对状态、图像哈希、RGB保真和尺寸，不能独自判定某像素是不是旧盘沿。隐藏/移开全部菜品时，应完整不透明、无旧菜品残留；数值通过仍不能豁免视觉验收。已知残留盘沿、抠图杂点的试验稿不能复用成已通过案例。

模型背景该处已修好而最终合成仍有旧物时，先修repair_region或protected_regions，再复用该背景prepare-background。重新审核绑定新scene哈希后构建。具体重试决策见批量与复用流程。
