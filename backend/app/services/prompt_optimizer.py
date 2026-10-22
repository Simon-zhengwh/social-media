from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from app.config import Settings
from app.schemas import PromptOptimizeRequest
from app.services import brand_style, platform_presets
from app.services.core_ai import CoreAIError
from app.services.image_ref import ImageRef, cleanup_image_refs, image_ref_from_asset


class PromptOptimizerError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


SYSTEM_PROMPT = (
    "你是资深社媒视觉创意总监、商业海报美术指导和图像生成提示词编辑。"
    "你必须根据用户当前提示词、品牌规范、平台规则以及提供的图片，输出一份可直接用于生图的中文提示词。"
    "只输出一个合法 JSON 对象，格式为 {\"prompt\":\"...\"}，不要 Markdown、解释或代码块。\n"
    "分析要求：\n"
    "1. 先识别每张素材图中的人物、产品、动作、场景、品牌元素和可见文字；对促销图、邀请函等信息图要做准确 OCR。\n"
    "2. 当前提示词为空时，从素材和上下文中生成完整初稿；非空时，以当前提示词为意图来源，优化结构、视觉方向和可执行性，不要改成无关需求。\n"
    "3. 用户已给出的标题、价格、原价、优惠、日期、地址、电话、预订方式等事实必须原文保留；禁止新增任何未提供的业务数字、承诺、联系方式或活动信息。\n"
    "4. 平台规则和品牌规范优先于通用审美建议。平台禁止的内容不得写进提示词，也不要用占位符表达。\n"
    "5. 提示词最多 1800 个中文字符，结构应包含：素材保真与主体、整体风格、构图与视觉层级、配色/字体/装饰、准确文案、平台适配和禁止项。\n"
    "6. 用户明确要求的文案必须可定位、可排版；不要用“自行发挥文案”替代已给出的事实。\n"
    "7. 如果素材图是人物或成品照片，要求保留人物身份、动作、工具和产品细节，说明如何以拼贴/融合方式使用，禁止原样矩形硬贴或拉伸换脸。\n"
    "8. 输出开头必须增加【事实锁定】区，列出从用户提示词和素材 OCR 中得到的关键价格、原价、优惠、日期、品牌、预订备注；这些文字后续只能逐字复制，禁止改写、换算币种或新增相近数字。\n"
    "9. 保留最佳实践：真实餐饮摄影和主理人手作拼贴感；深绿/品牌红/奶油白/暖黄配色；毛笔主标题、手写批注、撕纸和少量装饰；明确的人物—产品—价格三角层级；底部规则文字使用足够字号与行距；素材二信息要转译为清晰模块而不是原样贴图。\n"
    "10. 事实锁定的关键项必须在最终提示词中再次以引号原文出现；不得只写“保存价格和日期”这类抽象要求。\n"
    "11. 稳健细节：次要手写批注必须有足够字重或浅色衬底；快闪等次级活动信息不能缩到不可读；人物手部、工具、衣物和机器边缘要注明原生像素优先、避免AI重绘；底部条款要用清晰字号与行距。"
)


def _unique_ids(values: list[str], label: str) -> list[str]:
    normalized = [str(value).strip() for value in values if str(value).strip()]
    if len(set(normalized)) != len(normalized):
        raise PromptOptimizerError("duplicate_asset", f"{label}不能重复", 422)
    return normalized


def _validate_products(db: Any, asset_ids: list[str]) -> list[str]:
    if not 1 <= len(asset_ids) <= 4:
        raise PromptOptimizerError("product_required", "请先选择 1–4 张参考素材", 422)
    for asset_id in asset_ids:
        asset = db.assets.find_one({"_id": asset_id})
        if asset is None or asset.get("kind") not in {"product", "logo"}:
            raise PromptOptimizerError("product_missing", "参考素材不存在或类型不正确", 422)
    return asset_ids


def _validate_style_refs(db: Any, asset_ids: list[str]) -> list[str]:
    if len(asset_ids) > 3:
        raise PromptOptimizerError("style_reference_limit", "风格参考图最多 3 张", 422)
    for asset_id in asset_ids:
        asset = db.assets.find_one({"_id": asset_id})
        if asset is None or asset.get("kind") != "brand_reference":
            raise PromptOptimizerError("style_reference_missing", "风格参考图不存在或类型不正确", 422)
    return asset_ids


def _platform_context(request: PromptOptimizeRequest) -> str:
    if not request.platform_id:
        return platform_presets.freeform_context()
    try:
        platform, placement = platform_presets.resolve_placement(
            request.platform_id, request.placement_id
        )
    except platform_presets.PlatformError as exc:
        raise PromptOptimizerError("invalid_platform", exc.message, 422) from exc
    merchant = request.merchant_info.model_dump() if request.merchant_info else None
    return platform_presets.prompt_context(platform, placement, merchant)


def _image_data_url(value: ImageRef, *, max_side: int = 720) -> str:
    if value.url:
        return value.url
    if value.path is None:
        raise PromptOptimizerError("invalid_reference", "参考图没有可用的本地路径或地址", 422)
    try:
        with Image.open(value.path) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
            image = image.convert("RGB")
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=82, optimize=True)
    except Exception as exc:  # noqa: BLE001 - uploaded image is untrusted
        raise PromptOptimizerError("invalid_reference", f"参考图无法读取：{value.name}", 422) from exc
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _load_reference(asset_id: str, db: Any, store: Any) -> ImageRef:
    try:
        return image_ref_from_asset(db, store, asset_id)
    except Exception as exc:  # noqa: BLE001 - surface actionable user error
        raise PromptOptimizerError("reference_unavailable", str(exc), 422) from exc


def optimize_prompt(
    db: Any,
    settings: Settings,
    store: Any,
    client: Any,
    *,
    request: PromptOptimizeRequest,
    revision_guidance: str | None = None,
) -> str:
    product_ids = _unique_ids(request.product_asset_ids, "参考素材")
    _validate_products(db, product_ids)

    brand: brand_style.BrandStyle | None = None
    style_reference_ids: list[str] = []
    if request.brand_mode == "preset":
        if not request.brand_id:
            raise PromptOptimizerError("brand_required", "请选择预设品牌风格", 422)
        try:
            brand = brand_style.get_brand(db, request.brand_id)
        except brand_style.BrandError as exc:
            raise PromptOptimizerError("brand_missing", exc.message, 422) from exc
    elif request.brand_mode == "reference":
        style_reference_ids = _validate_style_refs(
            db, _unique_ids(request.style_reference_asset_ids, "风格参考图")
        )

    references: list[ImageRef] = []
    try:
        for asset_id in product_ids:
            references.append(_load_reference(asset_id, db, store))
        if brand is not None:
            for asset_id in brand.reference_asset_ids[:brand_style.MAX_BRAND_REFERENCES]:
                references.append(_load_reference(asset_id, db, store))
        elif request.brand_mode == "reference":
            for asset_id in style_reference_ids:
                references.append(_load_reference(asset_id, db, store))

        platform_text = _platform_context(request)
        brand_text = ""
        if brand is not None:
            brand_text = (
                f"品牌：{brand.name}\n"
                f"品牌摘要：{brand.summary}\n"
                f"品牌排版规范：\n{brand.layout_guide}\n"
                f"品牌摄影与背景规范：\n{brand.background_guide}"
            )

        workflow_text = (
            "目标工作流：自由设计（整图直接生成）。"
            "提示词必须描述一张已经完成排版、文字和装饰的最终海报。"
        )
        current_prompt = request.prompt.strip()
        prompt_context = (
            "当前提示词为空。请直接生成一版完整海报提示词。"
            if not current_prompt
            else f"当前提示词：\n{current_prompt}"
        )
        revision_text = ""
        if revision_guidance and revision_guidance.strip():
            revision_text = (
                "\n这是基准迭代模式。请以当前最高分提示词为基础，只做高影响改进；"
                "保留已经正确的构图、事实和文案，优先修复以下评审问题：\n"
                f"{revision_guidance.strip()}"
            )

        context_parts = [
            workflow_text,
            prompt_context,
            brand_text,
            platform_text,
            revision_text,
        ]
        context = "\n\n".join(part for part in context_parts if part).strip()
        content: list[dict[str, Any]] = [{"type": "text", "text": context}]

        if request.brand_mode == "reference" and style_reference_ids:
            content.append(
                {
                    "type": "text",
                    "text": "以下图片是风格参考图。只参考其配色、构图、字体气质和氛围，不复制其中文字、价格、人物或 Logo。",
                }
            )

        for index, reference in enumerate(references, start=1):
            if index <= len(product_ids):
                label = f"素材图 {index}：用于识别主体、人物、产品、动作、场景和可见文字。"
            elif brand is not None:
                label = (
                    f"品牌参考图 {index - len(product_ids)}：用于学习品牌视觉语言，"
                    "不复制其中的业务文字。"
                )
            else:
                label = (
                    f"风格参考图 {index - len(product_ids)}：只参考其配色、构图、字体气质和氛围，"
                    "不复制其中的业务文字。"
                )
            content.append({"type": "text", "text": label})
            content.append({"type": "image_url", "image_url": {"url": _image_data_url(reference)}})

        raw = client.chat_json(
            [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}],
            model=settings.core_ai_design_model,
            timeout_seconds=settings.llm_timeout_seconds,
        )
        prompt = raw.get("prompt") if isinstance(raw, dict) else None
        if not isinstance(prompt, str) or not prompt.strip():
            raise CoreAIError("prompt_optimizer_empty", "提示词模型没有返回有效提示词", 502)
        prompt = prompt.strip()
        if len(prompt) > 5000:
            raise CoreAIError(
                "prompt_optimizer_too_long",
                "优化后的提示词超过 5000 字，请重试",
                502,
            )
        return prompt
    finally:
        cleanup_image_refs(references)
