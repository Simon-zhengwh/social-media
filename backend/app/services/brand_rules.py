from __future__ import annotations

from typing import Any

from app.services import platform_presets

DEFAULT_RULES = {
    "version": 1,
    "copy_accuracy": "准确呈现用户指定文字，不编造价格、规则或承诺；涉及优惠时保留完整条件，多个活动分别对应日期与条件。",
    "asset_usage": "内容素材决定主体身份与关键细节；风格参考只用于视觉表达，不带入旧商品、旧文案或其他品牌标识。",
    "logo_usage": "使用提供的 Logo 时保持图形、字形、比例和原色，不用生成文字替代。",
    "readability": "文字与装饰避开关键主体、脸部和 Logo，不裁切必要信息。",
}
RULE_LABELS = {"copy_accuracy": "文案准确", "asset_usage": "素材使用", "logo_usage": "Logo 使用", "readability": "可读性"}
STYLE_LABELS = {
    "palette_notes": "配色说明", "typography": "字体风格", "subject_style": "主体表现",
    "background": "背景与质感", "layout": "排版偏好", "decoration": "装饰元素",
    "identity": "固定识别元素", "restrictions": "专属禁忌与补充",
}


def get_rules(db: Any) -> dict[str, Any]:
    document = db.brand_rules.find_one({"_id": "default"}) if db is not None else None
    source = document or DEFAULT_RULES
    return {key: source.get(key, value) for key, value in DEFAULT_RULES.items()}


def rules_context(rules: dict[str, Any] | None) -> str:
    if not rules:
        return ""
    return "通用规范（必须遵守）：\n" + "\n".join(
        f"- {label}：{rules[key]}" for key, label in RULE_LABELS.items() if rules.get(key)
    )


def compile_style_fields(style_fields: dict[str, Any]) -> str:
    return "\n".join(f"{label}：{str(style_fields.get(key) or '').strip()}" for key, label in STYLE_LABELS.items() if str(style_fields.get(key) or '').strip())


def brand_context(brand: dict[str, Any]) -> str:
    guides = compile_style_fields(brand["style_fields"]) if brand.get("style_fields") else "\n".join(dict.fromkeys(str(brand.get(key) or "").strip() for key in ("layout_guide", "background_guide")))
    colors = "、".join(str(color.get("hex") or "") for color in brand.get("colors") or [])
    return "\n".join(part for part in (
        f"品牌：{brand.get('name') or ''}", f"风格定位：{brand.get('summary') or ''}",
        f"品牌主色调（HEX）：{colors}" if colors else "", guides,
    ) if part)


def compile_brand_preview(db: Any, brand: dict[str, Any]) -> str:
    platform, placement = platform_presets.resolve_placement(brand.get("platform_id") or brand.get("platform"), brand.get("placement_id"))
    return "\n\n".join((rules_context(get_rules(db)), brand_context(brand), platform_presets.prompt_context(platform, placement)))
