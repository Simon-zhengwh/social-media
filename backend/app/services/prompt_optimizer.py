from __future__ import annotations

import json
from typing import Any

from app.config import Settings
from app.schemas import PromptOptimizeRequest
from app.services import brand_rules, brand_style, platform_presets
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
    if not 1 <= len(asset_ids) <= 6:
        raise PromptOptimizerError("product_required", "请先选择 1–6 张素材图", 422)
    for asset_id in asset_ids:
        asset = db.assets.find_one({"_id": asset_id})
        if asset is None or asset.get("kind") not in {"product", "logo"}:
            raise PromptOptimizerError("product_missing", "素材图不存在或类型不正确", 422)
    return asset_ids


def _validate_style_refs(db: Any, asset_ids: list[str]) -> list[str]:
    if len(asset_ids) > 3:
        raise PromptOptimizerError("style_reference_limit", "风格参考图最多 3 张", 422)
    for asset_id in asset_ids:
        asset = db.assets.find_one({"_id": asset_id})
        if asset is None or asset.get("kind") != "brand_reference":
            raise PromptOptimizerError("style_reference_missing", "风格参考图不存在或类型不正确", 422)
    return asset_ids


def _validate_logo(
    db: Any,
    asset_id: str | None,
    brand: brand_style.BrandStyle | None,
) -> str | None:
    if not asset_id:
        return None
    asset = db.assets.find_one({"_id": asset_id})
    if asset is None or asset.get("kind") != "logo":
        raise PromptOptimizerError("logo_missing", "Logo 不存在或类型不正确", 422)
    if brand is not None and asset_id not in brand.logo_asset_ids:
        raise PromptOptimizerError(
            "logo_not_in_brand", "所选 Logo 不属于当前品牌风格", 422
        )
    return asset_id


def _platform_context(request: PromptOptimizeRequest) -> str:
    if not request.platform_id:
        return platform_presets.freeform_context()
    try:
        platform, placement = platform_presets.resolve_placement(
            request.platform_id, request.placement_id
        )
    except platform_presets.PlatformError as exc:
        raise PromptOptimizerError("invalid_platform", exc.message, 422) from exc
    return platform_presets.prompt_context(platform, placement)


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
    product_ids = _unique_ids(request.product_asset_ids, "素材图")
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

    if brand is not None and brand.platform_id:
        if request.platform_id and request.platform_id != brand.platform_id:
            raise PromptOptimizerError(
                "brand_platform_mismatch", "品牌风格与发布平台不匹配", 422
            )
        if (
            request.placement_id
            and brand.placement_id
            and request.placement_id != brand.placement_id
        ):
            raise PromptOptimizerError(
                "brand_purpose_mismatch", "品牌风格与发布用途不匹配", 422
            )

    logo_asset_id = _validate_logo(db, request.logo_asset_id, brand)

    if not settings.cos_enabled or not isinstance(store, CosObjectStore):
        raise PromptOptimizerError("cos_required", "提示词优化需要先配置腾讯云 COS", 503)

    platform_request = request
    if brand is not None and brand.platform_id and not request.platform_id:
        platform_request = request.model_copy(update={
            "platform_id": brand.platform_id,
            "placement_id": request.placement_id or brand.placement_id,
        })
    platform_text = _platform_context(platform_request)
    brand_text = ""
    if brand is not None:
        if brand.background_guide.strip() == brand.layout_guide.strip():
            guide_text = f"品牌生成规范：\n{brand.layout_guide}"
        else:
            guide_text = (
                f"品牌排版规范：\n{brand.layout_guide}\n"
                f"品牌摄影与背景规范：\n{brand.background_guide}"
            )
        brand_text = (
            f"品牌：{brand.name}\n"
            f"品牌摘要：{brand.summary}\n"
            f"品牌用途：{brand.purpose or '未指定'}\n"
            f"品牌主色调（HEX）：{brand_style.color_context(brand.colors) or '未指定'}\n"
            f"{guide_text}"
        )

    current_prompt = request.prompt.strip()
    prompt_context = (
        "当前提示词为空。请直接生成一版完整海报提示词。"
        if not current_prompt else f"当前提示词：\n{current_prompt}"
    )
    revision_text = ""
    if revision_guidance and revision_guidance.strip():
        revision_text = f"本次修改要求：\n{revision_guidance.strip()}"
    input_parts = [
        "目标工作流：海报设计（整图直接生成）。提示词必须描述已完成排版的最终海报。",
        prompt_context, brand_rules.rules_context(brand_rules.get_rules(db)), brand_text, platform_text, revision_text,
        "图片清单中 content_images 是内容素材，style_images 是风格参考，logo_images 是必须保留的商家标识；保留各图 label 与角色。"
        "风格参考只参考配色、构图、字体气质和氛围。"
        "Logo 必须描述真实标识内容、比例和原色，不复制风格参考中的其他标识。"
        "下游会附 content_images 和所选 logo_images，请将风格观察转化为提示词中的视觉描述。"
        "按 image-gen-prompt skill 编写提示词，返回 JSON：{\"prompt\":\"完整提示词\"}。",
    ]
    content_images: list[dict[str, str]] = []
    style_images: list[dict[str, str]] = []
    logo_images: list[dict[str, str]] = []
    expires = max(settings.cos_presign_expiry_seconds, settings.core_ai_prompt_agent_timeout_seconds + 60)
    for index, asset_id in enumerate(product_ids, start=1):
        url = _cos_reference_url(db, store, asset_id, expires)
        content_images.append({"label": f"素材图 {index}", "url": url})
    brand_ids = brand.reference_asset_ids[:brand_style.MAX_BRAND_REFERENCES] if brand else []
    for index, asset_id in enumerate(brand_ids, start=1):
        url = _cos_reference_url(db, store, asset_id, expires)
        style_images.append({"label": f"品牌参考图 {index}", "url": url})
    if request.brand_mode == "reference":
        for index, asset_id in enumerate(style_reference_ids, start=1):
            url = _cos_reference_url(db, store, asset_id, expires)
            style_images.append({"label": f"风格参考图 {index}", "url": url})
    if logo_asset_id:
        logo_images.append({
            "label": "商家 Logo（必须保留）",
            "url": _cos_reference_url(db, store, logo_asset_id, expires),
        })
    input_parts.append("图片清单（JSON）：\n" + json.dumps(
        {
            "content_images": content_images,
            "style_images": style_images,
            "logo_images": logo_images,
        },
        ensure_ascii=False,
    ))

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
