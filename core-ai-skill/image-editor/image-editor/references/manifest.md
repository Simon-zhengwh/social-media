# 编辑器 manifest

本文件用于 `photos`（照片窗口）、`background`（整张摄影背景）、`elements`（元素拆层）的HTML。背景专用示例见[背景流程](background.md)。菜品自由RGBA图层使用[菜品manifest](dishes.md)和build_dish_editor.py；两阶段改字使用[改字manifest](text-edit.md)和package_text_edit.py，不套照片窗口字段。

路径相对manifest；坐标以最终HTML画布为准。输入PNG、JPEG、WebP等光栅图，不接受HTML/SVG伪装成原照。构建只读输入，输出目录必须不存在或为空。

## 默认照片模式

```json
{
  "title": "海报原照调整",
  "mode": "photos",
  "poster": "poster.png",
  "template": "generated-foreground.png",
  "canvas_size": [1080, 1440],
  "generation": {"new_calls": 1, "upstream_calls": 0, "reports": ["template-generation.json"]},
  "slots": [
    {
      "id": "left-photo",
      "label": "左侧菜品照片",
      "source": "original-photo.jpg",
      "fit_box": [0,470,530,1440],
      "photo_core": {"polygon": [[100,800],[350,800],[350,1100],[100,1100]]},
      "source_crop": [0,0,1080,1920]
    }
  ]
}
```

示例只展示格式，不套用坐标。每张照片各写一个slot，数量不固定，不是只做一张可调底图。

用户明确要求完整保留原照、自行取景时，slot的polygon/mask仅覆盖旧菜品及其餐盘的局部显示窗口，海报木桌等已有摄影背景属于固定template。`source`使用完整原照，原照背景在局部窗口内由用户调整取景。一般的1到N个菜品替换使用dishes.md的完整背景与自由抠图图层。

- `poster`：原始完成海报，保留来源；默认用其EXIF校正尺寸作为画布。
- `template`：生图模型原生透明结果。photos直接使用完整映射RGBA作为固定层，绝不从原稿恢复RGB或alpha。默认不移除艺术字；设计保留由提示词和视觉验收负责，不声称原像素锁定。
- `canvas_size`：可省略，三种HTML模式都必须等于原海报EXIF校正尺寸。模板长宽比偏差超过0.2%直接报错；同长宽比的分辨率变化映射到完整画布，仅容忍极小像素取整误差。没有contain留边、自动裁边或大幅拉伸兜底。定位窗口必须以原海报画布为准，核对生成结果是否保持同一构图；长宽比通过不证明局部边界对齐。
- `id`：字母开头的小写ASCII/数字/连字符，各照片和元素全局唯一。
- `polygon` 或 `mask`：多窗口时二选一。单窗口photos可同时省略，整个原照放在模型模板下方，由模板真实alpha决定最终可见区域；初始fit_box默认取照片显示区域（模板alpha<240）的外接框，也可指定。显式窗口只裁新原照，不影响固定层。polygon为至少三个画布坐标；mask为画布同尺寸灰度PNG，白色允许该照片显示，黑色禁止。RGBA门图须先取alpha为灰度mask，不能把白RGB当作mask。遮挡图形由固定层保留。
- 窗口在透明区域不许重叠。被固定实心分隔线盖住的共享边可由后声明窗口独占；最终裁切门不重叠。不要靠绘制顺序修正语义错误的窗口。
- `photo_core`：每个slot必需，格式为 `{"mask":"core.png"}` 或 `{"polygon":[...]}`。必须由原海报独立标注，非空且在该窗口内，并覆盖明确的旧菜、容器/盘沿等区域；多个不连通内核用灰度mask联合。映射模板在这些像素必须alpha全0，残留alpha=1也拒绝。不要从模型alpha反推、缩小或删除内核绕过检查。
- `protected_regions`：顶层可选列表，各项为 `{"id":"price-badge","mask":"price-protection.png"}` 或同格式polygon。photos中只检查模型模板未清空这些必须保留的设计，不贴回原海报；缺失直接拒绝。不得与photo_core重叠，不能保护旧食物/盘沿。elements/background仍保留已有显式原稿保护合成功能，使用时另行验收。
- `fit_box`：可选 `[left,top,right,bottom]`，默认mask外接框。决定初始照片摆放中心和铺满尺寸；必须实际覆盖需要显示的区域，不要因旧标题遮挡就只铺一小块。
- `source_crop`：完整原照中的初始取景框，默认全图；只用它计算初始位置/比例。原图字节完整内嵌，用户仍能移入框外内容。
- `background`：可选完整不透明底图。照片模式没有指定底图时，空窗口允许透明；用户要求移走照片后也不能露空时必须提供。元素模式必需，见下。

## 仅明确授权的元素模式

在上例基础上改为：

```json
{
  "mode": "elements",
  "element_request": "用户明确要求将右上标语和牛肉标签拆成可拖动贴纸",
  "template": "fixed-without-selected-elements.png",
  "background": "complete-opaque-background.png",
  "elements": [
    {"id":"crowd-slogan","label":"右上标语","source":"crowd.png","text":"原海报准确文案","box":[790,20,1070,205]},
    {"id":"fresh-label","label":"鲜切牛肉标签","source":"fresh.png","box":[710,460,990,640]}
  ]
}
```

这是字段增量，仍需poster、template等必需项。有照片时提供slots，每个仍须独立photo_core；纯设计海报允许 `slots: []` 或省略slots。默认 `photos` 下出现elements会被拒绝；元素模式缺少 `element_request`、元素PNG或完全不透明的background也会拒绝。

- `source`必须是真实含透明边缘且有可见图案的PNG；保留原生alpha，不阈值去底。
- `box`是该元素在画布的初始目标框，整组等比contain，保持元素原始内部构图。可先裁掉完全透明外边距再加透明安全边，但不能裁字、删装饰或重采样成假矢量。
- `template`只保留未拆走的固定设计，不能包含所列elements的第二份副本。
- `background`是整个画布的不透明底层。HTML中等比cover铺满，不拉伸；照片和固定层在上，元素最后绘制。
- 元素不限艺术字，也可为Logo、插画、分隔线、独立装饰。用户要求全拆时将这些也写入清单；同一完整文案与衬底默认作为一组，不自动逐字拆。
- 官方 Logo 元素使用 `role: "logo"`，直接内嵌其原文件（可透明或不透明），保持完整图案与原色，按 box 等比 contain。此角色不受艺术字必须透明的要求限制；模板中不能再有旧 Logo 副本。编辑器提供独立“更换 Logo”入口。
- 若没有剩余固定设计，可以用Pillow创建原画布同尺寸全透明RGBA作为template；这是空结构层，不冒充模型生成结果，不必为它多调用一次模型。没有照片窗口时，固定层也允许不透明；有窗口则仍检查photo_core全透明。
- 从扁平海报拆层不能恢复真实源设计文件。无原始独立照片时，可先用原海报的照片面板裁图作为source，记录它是裁图且遮挡不可恢复；需要无损完整原照时再索取原照，不模型重绘后冒充原照。

## 生成记录

Core AI中 `generation.reports` 必须引用 `record_core_ai_generation.py` 从真实generate_image回复生成的完整记录，包含原样tool_result、实际链接/media_id及输入输出哈希。下面只解释构建器读取的通用字段，不是让Agent手写或伪造简化收据；命令见[平台运行](platform-runtime.md)。不要臆造供应商endpoint或返回状态：

```json
{"tool":"实际工具名","purpose":"photo-template","status":"success","prompt":"完整实际提示词","output":"生成PNG路径","sha256":"实际文件SHA256","attempts":1}
```

`new_calls`记录本轮所有实际请求，失败尝试也计入；`upstream_calls`记录复用产物的已知上游请求。未知历史写在记录备注，不编造数字。构建器不调用模型，导出的复跑manifest将new_calls置0，把已知调用移到upstream_calls。

## 构建产物与复跑

`editor.html`内嵌全部图片与脚本，不需要服务、CDN、图床或外链。`inputs/`保留原始来源字节，`poster-template.png`为映射模板，`*-mask.png`为独立裁切门，`manifest.json`路径已改为包内相对路径。整个结果目录移动后仍可用同一skill的构建脚本复跑。

`report.json`记录尺寸映射、输入哈希、模型调用声明和验证状态。它不是视觉验收报告；浏览器检查另存JSON。

照片模式记录 `protection.original_pixels_locked: false`、`fixed_layer_source: model_template`、`model_template_changed_pixels: 0`、window_coverage及各photo_core的alpha。poster-template.png与generated-mapped-template.png的RGBA一致，分别是实际固定层与映射证据，不再声称窗口外原稿零变化。模型在原稿可见区域中alpha<240的照片显示范围必须被窗口联合覆盖（沿用实心分隔线的240判定，包含半透明边缘）；漏掉时拒绝构建，而不是填回旧照片。单张照片默认无需手绘精细门图。验收失败时不会写出HTML。

窗口覆盖阈值只决定新原照的承接范围，不改模型alpha；photo_core仍要求严格alpha=0。alpha>=240的近不透明设计/轻微噪点不用于扩大取景到全画布，边缘与图形保留情况仍需视觉验收。
