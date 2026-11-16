# 替换整张摄影背景

这里的“背景”指用户授权替换的完整摄影底图，可包含原菜品、容器、桌面与环境。若只要求换桌面而保留菜品，先保留菜品为独立层，不运行本流程的整层摄影移除。不要将普通换菜或局部照片窗口升级为整背景替换。

## 模型原生前景 + 完整新背景 + HTML

1. 原海报为唯一编辑目标。模型移除授权的全部旧摄影内容，生成仅保留文字、Logo、笔刷、贴纸、插画及边框的原生透明前景；不能用OpenCV、本地颜色提取或阈值去底代替。完整新背景原照不送去重绘，直接进入HTML。
2. 从原稿独立标注旧摄影验收内核，可用多个不连通区域的联合mask。检查每个设计组的文案、尺寸、位置、实心底与孔洞；隐藏新背景，核对整张前景没有旧摄影残片。生成前景可能改变字形，不承诺原稿RGB一致。
3. `mode: background` 使用恰好一个全画布窗口。构建器直接使用模型的原生RGBA，与照片窗口模式一样不从原稿恢复像素；区别在于本流程移除整张授权摄影底图。前景长宽比偏差超过0.2%或内核非全透明时拒绝构建。
4. HTML中背景原照可移动、等比缩放、更换，透明设计前景固定在上层；不再生成整张最终海报。原照移开后允许露透明，棋盘格不写入PNG。

## 提示词

```text
Use case: background-extraction.
The completed poster is the ONLY edit target. Remove the entire authorized photographic scene: [old food, all serving containers, tabletop/room, photographic shadows and reflections].
Keep ONLY the graphic design foreground: [exact text, logo, real solid backplates, brushwork, stickers, illustration, frame lines]. Keep white/cream graphic fills and letters opaque. Preserve full-canvas positions, sizes, wording, line breaks and aspect ratio; do not gather the groups into the center.
Output one native RGBA PNG with real alpha-zero pixels everywhere the old photographic scene was visible, including holes between letters and brush strokes. No old photographic remnants, replacement photo, mockup, painted checkerboard or watermark.
```

使用工具真实透明选项。完整原照不参与生成；只作为下层素材，不从它抽取主体。

## 记录与manifest

已完成生成后记录实际工具和完整提示词；脚本不调用模型：

```bash
python scripts/record_core_ai_generation.py --image work/foreground.png --tool-result work/foreground-tool-result.txt --model gpt-image-2.5-sunburst --background transparent --purpose background-foreground --prompt-file work/background-prompt.txt --reference work/poster.png --out work/foreground-generation.json --review-out work/foreground-review.json
```

审核记录初始PENDING，实际检查后填写notes及真实状态。报告不代替视觉验收；本流程构建器校验generation与文件绑定，视觉结论单独交付。

```json
{
  "mode":"background",
  "title":"背景原照调整",
  "poster":"poster.png",
  "template":"foreground.png",
  "slots":[{
    "id":"background-photo","label":"新背景原照","source":"new-scene.jpg",
    "polygon":[[0,0],[1080,0],[1080,1440],[0,1440]],
    "photo_core":{"mask":"independent-old-scene-core.png"}
  }],
  "generation":{"new_calls":1,"upstream_calls":0,"reports":["foreground-generation.json"]}
}
```

示例尺寸须替换为实际原稿尺寸。`template`必须有绑定其SHA256、`status: success`、`purpose: background-foreground`、真实tool和prompt的记录。原生生成文件完整保存，不阈值改alpha。多窗口用 `photos`，不要在background模式声明多张局部照片。通用路径、窗口与复跑规则见[manifest](manifest.md)。
