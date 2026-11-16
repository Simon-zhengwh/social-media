# Core AI 原生 generate_image 工具协议

由当前Agent直接调用原生工具，不是HTTP请求、Python SDK或调用另一个Agent。以下按备份版平台协议适配；**执行时以本会话实际工具声明为准**，不要把示例当作未经检查的固定schema。未暴露时通过实际可用的activate_tools激活，仍缺失则停止相应生成阶段。

## 参数与图片引用

- `prompt`：实际完整提示词，写明每张图角色、目标位置、改动与不变项。各模式的具体内容见model-prompts、dishes、background、text-edit。
- `model`固定为gpt-image-2.5-sunburst，`quality`固定为xhigh，`n`为1，`output_format`为png。调用前核对当前工具支持这些值。参数不支持时报告差异并保留实际错误。
- `input_images`：备份版协议要求**JSON数组序列化后的字符串**，不是直接数组。成员使用真实 `media_id`、`sandbox_path` 或 `url`，可用name并在prompt明确角色。避免base64，不传本机Windows路径。
- `background`：透明前景/贴纸用transparent，菜品补背景及最终整图用opaque（或工具支持的等价不透明控制）；不能只在prompt写“透明”而省略可用的透明开关。若工具未暴露opaque选项，可按声明省略，并在收据用auto记录，结果仍须全不透明。
- `mask`：备份版协议为**单个对象序列化后的字符串**，不是数组；实际语义以工具声明为准。备份版为透明允许重绘、不透明保护。同网格对齐，不独立缩放mask；不要为了mask强行裁切真实贴纸或丢弃必需引用。
- `size`：只用受支持的原画布尺寸/比例，或按工具声明省略。PNG不传JPEG压缩参数；不机械套用auto、referenced_image_paths、num_last_images_to_include等其他工具参数。

`attached`仅在全部附件都应入模时使用。换背景同时收到海报和新背景原照时，不能用attached把新背景也送去重绘。`last`不是跨会话引用；多阶段优先显式media_id。

## 透明背景前景示例

占位值必须替换为当前真实值；不是可以原样提交的请求。

```json
{
  "prompt":"@poster是唯一编辑目标。移除用户授权的全部摄影底图，保留原文、Logo、实心笔刷与装饰及画布布局，仅输出原生透明设计前景。",
  "model":"gpt-image-2.5-sunburst",
  "quality":"xhigh","n":1,"output_format":"png","background":"transparent",
  "input_images":"[{\"sandbox_path\":\"<实际海报沙箱路径>\",\"name\":\"poster\"}]"
}
```

## 两阶段改字的第二阶段示例

第一阶段只引用原海报生成实际新字透明PNG；审核通过后第二阶段必须引用原海报及**该已验贴纸**，不能仅靠prompt说“用上一张图”。

```json
{
  "prompt":"@poster为原海报，@lettering为已验新字贴纸。仅替换指定旧字，按原目标位置和大小映射贴纸，清掉旧字残边，其他摄影、设计和画布保持。输出完整不透明PNG。",
  "model":"gpt-image-2.5-sunburst","quality":"xhigh","n":1,"output_format":"png","background":"opaque",
  "input_images":"[{\"sandbox_path\":\"<原海报实际路径>\",\"name\":\"poster\"},{\"media_id\":\"<贴纸实际media_id>\",\"name\":\"lettering\"}]"
}
```

## 返回与失败

备份版成功结果形如 `Image generated.`、一条Markdown图片链接和可选media_id，不是HTTP网关的data[0]。原样保存文本，下载/读取实际原生PNG，再用record_core_ai_generation.py记录。若当前平台返回结构发生变化，先检查真实返回与工具声明；不能伪造旧格式通过记录器，保留证据并报告适配差异。

透明alpha、尺寸、文字与视觉质量分开验收。原生透明失败不本地去底冒充；菜品OpenCV只处理原照，是独立授权流程。400先诊断参数和模型能力，不重复未改请求；超时未知先查原请求，不盲目重发。可下载原生文件不等于素材验收通过。
