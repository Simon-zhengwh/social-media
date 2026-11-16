# 模型提示词

每次标明图片角色、精确移除范围和不变项。通过Core AI原生generate_image传入实际input_images引用，并设置工具真实的透明选项；先读[工具协议](tool-interface.md)。JSON字符串和平台引用不能套用本机imagegen参数，本包不依赖另一份skill或图片HTTP网关。

## 照片窗口模式的海报挖空

本节只用于独立照片面板或明确不抠原照的窗口分支。替换1到N个菜品使用 [菜品流程](dishes.md)：完整不透明背景 + OpenCV抠原照，不生成这里的透明菜品洞。

将下面文字替换成实际照片区域与用户文案。已有图片在会话中就不重新注入大图。

先写明替换范围类型：局部菜品、独立照片面板或用户明确要求的背景。局部菜品必须列出要保留的原海报摄影背景，不能套用“删掉全画布摄影内容”。原照只作识别参考，既不重绘也不抠主体。

仅靠提示词要求“其他不变”不能证明模型保留原像素。照片模式直接采用已验收模型透明模板的完整RGBA，须视觉核对固定设计；不得用原稿RGB或窗口外原始alpha覆盖模型结果。先从原海报独立标注旧照片验收内核，不以生成alpha作为验收内核依据。单张照片可不手绘精细裁切门，直接用模板透明区显示；多张照片分别分配窗口并覆盖全部应换图区域。模板的非授权透明、设计错位或内核非透明都应实际修正，不能贴回旧海报或靠新照片遮盖。

```text
Use case: background-extraction
Asset: reusable transparent poster foreground overlay.
Inputs: Image 1 is the completed poster and ONLY edit target.
Images 2..N are source-photo references to identify the photographic regions to remove; do not insert their content.
Edit scope: [local dish / explicit photo panel / explicitly requested background].
Remove exactly: [enumerate authorized target regions and their boundaries]. For local dish replacement, remove only the old food AND its serving container within the local target window. Preserve the poster's existing tabletop, room, spices and all scenery outside that local window, even though they are photographic. Remove a photo panel's full internal scene only when that entire panel is the authorized target. Do not infer full-canvas background removal.
Keep this existing photographic background fixed: [explicit wood tabletop / wall / surrounding spices / other unchanged regions]. Do not turn it transparent or replace it with the source photo's backdrop. Do not create an additional header-background window.
Keep every other element from Image 1: exact wording, logo, typography, colors, paint-swash backplates, stickers, decorative icons, leaves and thin photo-frame separators. Preserve their sizes and positions. Keep solid cream/white sticker fills and white letters opaque.
Where a graphic overlaps a photo, preserve its visible graphic pixels and make the uncovered photographic pixels around/within it truly transparent. No old photo fragments in brush holes or between letters.
Preserve the original full-canvas aspect and framing; do not center or gather the graphic groups together. No redesign, no new wording, no source-video subtitles imported.
Output one native RGBA PNG with actual alpha-0 photo windows. No replacement photographs, no painted checkerboard, no solid white/black fill in the windows, no new objects or watermark.
```

默认一张模板请求即可；不要对每张原照都重画一份完整海报。

例，卤鸡海报＋灰底卤鸡原照，用户明确要求完整保留原照并自行取景。此时只挖旧鸡和盘子的局部窗口，木纹桌面、八角辣椒香菜和设计固定。HTML将完整灰底原照放在局部窗口下，用户调位置与缩放。一般的1到N个菜品替换则按dishes.md制作完整背景与OpenCV抠图。

## 明确要求拆元素：单组贴纸

```text
Use case: background-extraction
Input: existing poster/foreground is the edit target.
Extract ONLY [specific group at specific location].
Exact text: "[verbatim original or explicitly requested new wording]".
Include [its complete brushstroke/solid color backplate and associated icons]. Exclude all neighboring design groups, photos and panel borders.
Preserve original lettering personality, line breaks, internal layout, palette, tilt and rough paint edges. Keep all lettering and solid backplates opaque. Do not crop brush tips, hearts, arrows or rays.
Return one complete isolated sticker, tightly framed with transparent safety margin; genuine RGBA transparency outside the sticker. No mockup, no backdrop rectangle, no labels, no painted checkerboard, no extra words.
```

每个独立组分别调用并保存PNG；不要生成一张包含所有组的平铺拼图冒充独立文件。

## 明确要求拆元素：干净固定层

```text
Use case: precise-object-edit
Input: completed original poster OR an already available poster foreground; preserve its full-canvas framing. Do not generate an intermediate complete foreground just to use this prompt.
If starting from the original completed poster, also remove ONLY these authorized photographic regions: [local dish windows or explicit photo panels]. Preserve existing photographic background outside those regions. Source photos are identification references only; do not remove their backdrop or extract a subject from them.
Remove completely these selected groups: [every group plus its paint backplate and associated decoration]. Do not leave a second copy or pieces of their lettering below the movable sticker.
Keep the unselected fixed elements AND any unchanged photographic background: [explicit logo, fixed text, corner panels, white photo separators, existing wood tabletop and scene outside authorized windows etc.]. Keep original sizes and full-canvas positions. Restore the obscured fixed-panel edges where needed without putting new text there.
Photo windows and areas outside retained fixed graphics remain genuinely transparent. Do not import old photographs or residues of the removed slogans.
Return full-canvas RGBA PNG, no painted checkerboard, no added words or decoration.
```

## 需要补全底层：不透明底图

```text
Use case: precise-object-edit
Asset: full-bleed opaque background underneath all editable photo/art layers.
Reference: poster fixed design as a palette/texture reference only.
Create a continuous finished [brand palette/material/texture] background across the WHOLE canvas, especially under [areas previously obscured by removable stickers]. Keep the existing style. Do not bake in any typography, logos, separators, source-photo subjects or movable stickers: these already exist as separate layers.
Every canvas pixel must be painted and opaque, edge to edge. No alpha holes, checkerboard pattern, blank margins, new slogans or watermark. Preserve portrait/landscape canvas aspect.
```

此处必须关闭工具的透明输出。颜色/纹理由实际海报决定，不固定使用喜聚绿色。已有完整合格底图时直接复用，不为满足固定调用数而重复生成。
