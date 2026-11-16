# 精准改字：透明新字贴纸 → 模型替换成图

适用于改一个字、词或指定句子。先确认原文、新文和准确位置；用户转述的不同措辞不自动视作改字授权。目标只需改一个字时，不为方便重做整个标题或全海报。

## 第一阶段：新字贴纸

直接用Core AI原生generate_image工具，以原海报为参考，生成包含实际新文案及必要笔刷/纸条衬底的原生透明PNG。保留原字形风格、颜色、角度、间距及组内布局，不只是提取旧字；透明范围不包括需要保留的实心衬底。

```text
Use case: text-edit. The supplied original poster is the reference.
At [original-canvas coordinates and exact target], replace "[old]" with "[new]".
Return ONLY the new typography sticker and its necessary original-style backplate, with native RGBA transparency outside it. Preserve [font personality, palette, tilt, brush edge, line spacing]. Do not include neighboring words or photographic pixels; do not crop letters or brush tips. No mockup, checkerboard or extra text.
```

记录并创建PENDING审核文件：

```bash
python scripts/record_core_ai_generation.py --image work/sticker.png --tool-result work/sticker-tool-result.txt --model gpt-image-2.5-sunburst --background transparent --purpose text-sticker --prompt-file work/sticker-prompt.txt --reference work/poster.png --out work/sticker-generation.json --review-out work/sticker-review.json
```

实际检查新文案逐字正确、字形/衬底/完整边缘，再填写review的notes并将status改为PASS。OCR结果不能代替视觉检查。第一阶段未通过时不进行第二阶段。

## 第二阶段：移除旧字并替换

直接用同一可用图片编辑工具，显式引用**原海报和已验新贴纸**。若使用工具支持的编辑mask，按该工具的真实mask语义设置，不假定白黑或alpha含义。记录原画布目标框与贴纸可见bbox，不能将被模型放大居中的贴纸整画布直接覆盖。

```text
Use case: precise-text-edit.
Image 1 is the original poster and ONLY edit target. Image 2 is the approved new-text sticker.
Replace ONLY "[old]" at [original target box] with the exact "[new]" from Image 2, mapped to [size, baseline, tilt]. Completely remove the old target letters and their edge remnants, restoring only the necessary immediately underlying background.
Keep all other text, photographic subjects, logo, decorations, colors, layout and original full-frame pixel dimensions unchanged. Do not redesign the title or redraw the food. Return one finished fully opaque PNG, not another transparent sticker.
```

最终PNG须由这次模型调用产生，不用本地粘贴或HTML截图冒充。保留原生文件；尺寸不等于原海报时先明确修正并重新验收，不直接拉伸、裁切或补边掩盖错误。

```bash
python scripts/record_core_ai_generation.py --image work/final.png --tool-result work/final-tool-result.txt --model gpt-image-2.5-sunburst --background opaque --purpose text-final --prompt-file work/final-prompt.txt --reference work/poster.png --reference work/sticker.png --out work/final-generation.json --review-out work/final-review.json
```

检查原字是否完整移除、新字位置与比例、周边摄影和设计是否漂移；最终审核单独填写PASS或FAIL。若要求像素级保留，模型成图不能自动提供这种保证，需核对差异并说明能力限制。

## 打包与复跑

```json
{
  "mode":"text",
  "poster":"poster.png",
  "old_text":"旧字","new_text":"新字",
  "edit_region":{"polygon":[[100,200],[350,200],[350,320],[100,320]]},
  "sticker":"sticker.png","final":"final.png",
  "sticker_generation":"sticker-generation.json",
  "final_generation":"final-generation.json",
  "sticker_review":"sticker-review.json",
  "final_review":"final-review.json",
  "generation":{"new_calls":2,"upstream_calls":0,"reports":["sticker-generation.json","final-generation.json"]}
}
```

坐标是原海报中允许改动及必要紧邻修补的完整范围，不能扩大范围来掩盖其他漂移。`edit_region`只用于记录和差异检查，不用于本地把贴纸贴回。`generation`记录实际次数，包括失败/废弃尝试；额外报告要一起保存。

```bash
python scripts/package_text_edit.py --manifest work/text.json --out work/text-result-v1
```

打包器核对原生PNG、透明贴纸、完全不透明最终图、原画布尺寸、两阶段记录的输入/输出哈希，以及带真实notes的PASS审核。review统一用 `image_sha256`；不能复用另一候选的审核。

交付 `final.png` 为主，附 `sticker.png`、原稿、edit-region、两阶段记录、审核、相对路径manifest和report。打包不再次生图、不重新合成；报告的 `outside_edit_changed_pixels` 是目标外像素变化数，可能非0，不能把生成结果声称为像素锁定。复跑manifest只重打包已存产物，不重做两次生成。
