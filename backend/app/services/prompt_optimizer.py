from __future__ import annotations

import json
from typing import Any

from app.config import Settings
from app.schemas import PromptOptimizeRequest
from app.services import brand_style, platform_presets
from app.services.core_ai import CoreAIError
from app.services.object_store import CosObjectStore


class PromptOptimizerError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


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


def _cos_reference_url(db: Any, store: CosObjectStore, asset_id: str, expires: int) -> str:
    asset = db.assets.find_one({"_id": asset_id})
    key = (asset.get("prepared_path") or asset.get("path")) if asset else None
    if not key:
        raise PromptOptimizerError("reference_unavailable", f"参考图不存在：{asset_id}", 422)
    try:
        # Uploaded assets already live in COS; a fresh signed URL makes them
        # readable to this run without duplicating the object.
        return store.presign(key, expires=expires)
    except Exception as exc:  # noqa: BLE001 - COS SDK errors vary
        raise PromptOptimizerError("reference_unavailable", "无法获取参考图的 COS 地址", 502) from exc


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

    if not settings.cos_enabled or not isinstance(store, CosObjectStore):
        raise PromptOptimizerError("cos_required", "提示词优化需要先配置腾讯云 COS", 503)

    platform_text = _platform_context(request)
    brand_text = ""
    if brand is not None:
        brand_text = (
            f"品牌：{brand.name}\n"
            f"品牌摘要：{brand.summary}\n"
            f"品牌排版规范：\n{brand.layout_guide}\n"
            f"品牌摄影与背景规范：\n{brand.background_guide}"
        )

    current_prompt = request.prompt.strip()
    prompt_context = (
        "当前提示词为空。请直接生成一版完整海报提示词。"
        if not current_prompt else f"当前提示词：\n{current_prompt}"
    )
    revision_text = ""
    if revision_guidance and revision_guidance.strip():
        revision_text = (
            "这是基准迭代模式。请以当前最高分提示词为基础，只做高影响改进；"
            "保留已经正确的构图、事实和文案，优先修复以下评审问题：\n"
            f"{revision_guidance.strip()}"
        )
    input_parts = [
        "目标工作流：自由设计（整图直接生成）。提示词必须描述已完成排版的最终海报。",
        prompt_context, brand_text, platform_text, revision_text,
        "请查看以下 COS 图片 URL。素材图用于识别主体和事实；品牌/风格图只用于视觉风格，不复制旧文案。",
    ]
    expires = max(settings.cos_presign_expiry_seconds, settings.core_ai_prompt_agent_timeout_seconds + 60)
    for index, asset_id in enumerate(product_ids, start=1):
        url = _cos_reference_url(db, store, asset_id, expires)
        input_parts.append(f"素材图 {index}（人物、产品、动作、场景与可见文字）：{url}")
    brand_ids = brand.reference_asset_ids[:brand_style.MAX_BRAND_REFERENCES] if brand else []
    for index, asset_id in enumerate(brand_ids, start=1):
        url = _cos_reference_url(db, store, asset_id, expires)
        input_parts.append(f"品牌参考图 {index}（只参考视觉语言）：{url}")
    if request.brand_mode == "reference":
        for index, asset_id in enumerate(style_reference_ids, start=1):
            url = _cos_reference_url(db, store, asset_id, expires)
            input_parts.append(f"风格参考图 {index}（只参考配色、构图、字体气质和氛围）：{url}")

    output = client.run_agent(
        settings.core_ai_prompt_agent_id,
        "\n\n".join(part for part in input_parts if part),
        timeout_seconds=settings.core_ai_prompt_agent_timeout_seconds,
    ).strip()
    if output.startswith("```"):
        output = output.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        raw = json.loads(output)
    except ValueError as exc:
        raise CoreAIError("prompt_optimizer_invalid_json", "提示词 Agent 没有返回有效 JSON") from exc
    if isinstance(raw, dict) and isinstance(raw.get("error"), str) and raw["error"].strip():
        raise CoreAIError("prompt_optimizer_agent_error", raw["error"].strip())
    prompt = raw.get("prompt") if isinstance(raw, dict) else None
    if not isinstance(prompt, str) or not prompt.strip():
        raise CoreAIError("prompt_optimizer_empty", "提示词 Agent 没有返回有效提示词")
    prompt = prompt.strip()
    if len(prompt) > 5000:
        raise CoreAIError("prompt_optimizer_too_long", "优化后的提示词超过 5000 字，请重试")
    return prompt
