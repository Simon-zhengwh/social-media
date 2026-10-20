from __future__ import annotations

import base64
import io
import json
import logging
import re
from pathlib import Path
from typing import Any

from PIL import Image

from app.config import Settings
from app.services.brand_style import DEFAULT_FONTS, DEFAULT_PALETTE, BrandStyle
from app.services import design as design_utils
from app.services.toapis import ToAPIsClient, ToAPIsError

logger = logging.getLogger(__name__)

ROLE_NAMES = {
    "headline": "主标题",
    "subheadline": "副标题",
    "price": "价格",
    "offer": "促销信息",
    "details": "活动信息",
    "cta": "行动号召",
    "brand": "品牌名",
    "legal": "补充说明",
}

ROLE_ORDER = [
    "brand",
    "headline",
    "subheadline",
    "offer",
    "price",
    "details",
    "cta",
    "legal",
]

STYLE_GUIDE = (
    "目标风格：高转化的中式餐饮社媒促销海报。真实菜品占据画面主体，"
    "红、绿、奶油白等品牌色强烈，超大粗体中文标题，清晰的价格/优惠层级，"
    "信息密度高但不遮挡菜品；Logo 小而精致，透明融入画面，不使用卡片底板。"
)


def _image_data_url(path: Path, max_side: int = 768) -> str:
    with Image.open(path) as source:
        image = source.convert("RGB")
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=82)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ToAPIsError("design_invalid_json", "设计模型没有返回 JSON 对象")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ToAPIsError("design_invalid_json", "设计模型返回的 JSON 无法解析") from exc
    if not isinstance(payload, dict):
        raise ToAPIsError("design_invalid_json", "设计模型返回内容不是对象")
    return payload


def _clean_copy(copy: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for role in ROLE_ORDER:
        value = copy.get(role)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            result[role] = text[:120]
    if not result.get("headline"):
        result["headline"] = "限时优惠"
    return result


def _fallback_copy(brief: str) -> dict[str, str]:
    quoted = re.findall(r"[“\"「『]([^”\"」』]{2,24})[”\"」』]", brief)
    headline = quoted[0] if quoted else ""
    if not headline:
        for keyword in (
            "开业",
            "火锅",
            "自助",
            "套餐",
            "新品",
            "限时",
            "特惠",
            "周末",
        ):
            if keyword in brief:
                headline = keyword
                break
    if not headline:
        headline = "人气美食"
    return {
        "headline": headline[:24],
        "subheadline": "新鲜食材 · 现场制作",
        "price": "限时优惠",
        "details": "欢迎到店品尝",
    }


def _clamp_box(
    raw: Any, canvas: dict[str, int], *, default: dict[str, int] | None = None
) -> dict[str, int]:
    width, height = canvas["width"], canvas["height"]
    fallback = default or {
        "x": int(width * 0.08),
        "y": int(height * 0.08),
        "width": int(width * 0.7),
        "height": int(height * 0.18),
    }
    if not isinstance(raw, dict):
        return dict(fallback)
    try:
        x = int(max(0, min(width - 20, float(raw.get("x", fallback["x"])))))
        y = int(max(0, min(height - 20, float(raw.get("y", fallback["y"])))))
        box_width = int(max(20, min(width - x, float(raw.get("width", fallback["width"])))))
        box_height = int(max(20, min(height - y, float(raw.get("height", fallback["height"])))))
    except (TypeError, ValueError):
        return dict(fallback)
    return {"x": x, "y": y, "width": box_width, "height": box_height}


def make_creative_plan(
    settings: Settings,
    client: ToAPIsClient,
    *,
    brief: str,
    product_paths: list[Path],
    has_logo: bool,
    brand_hint: str | None = None,
    brand: BrandStyle | None = None,
) -> dict[str, Any]:
    """Ask the design model for copy + a clean background prompt.

    The fallback keeps the pipeline usable when the vision model is unavailable.
    It never decides that generated text is acceptable in the background.
    """
    if settings.design_model_enabled and client is not None:
        try:
            return _llm_creative_plan(
                settings,
                client,
                brief=brief,
                product_paths=product_paths,
                has_logo=has_logo,
                brand_hint=brand_hint,
                brand=brand,
            )
        except Exception as exc:  # noqa: BLE001 - keep a deterministic fallback
            logger.warning("creative plan model failed: %s", exc)

    copy = _fallback_copy(brief)
    background_prompt = (
        brief.strip()
        or "高端火锅/中餐餐桌场景，暖色灯光，红绿品牌氛围，菜品有食欲，适合叠加促销文案。"
    )
    return {
        "creative_direction": brand.layout_guide if brand else STYLE_GUIDE,
        "background_prompt": background_prompt,
        "copy": copy,
        "source": "fallback",
    }


def _llm_creative_plan(
    settings: Settings,
    client: ToAPIsClient,
    *,
    brief: str,
    product_paths: list[Path],
    has_logo: bool,
    brand_hint: str | None,
    brand: BrandStyle | None,
) -> dict[str, Any]:
    layout_guide = brand.layout_guide if brand else STYLE_GUIDE
    brand_name = brand.name if brand else "未指定品牌"
    palette = brand.palette if brand else DEFAULT_PALETTE
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "你是资深商业海报美术指导。分析用户提供的菜品图和需求，"
                "决定海报文案与场景底图方案。\n"
                f"品牌：{brand_name}\n"
                f"品牌排版规范：\n{layout_guide}\n"
                f"品牌色板：{json.dumps(palette, ensure_ascii=False)}\n"
                "文案要求：中文，短促有力，符合真实商业促销，不得编造用户没有提供的价格、"
                "折扣或日期；如果用户没有给出，可写“限时优惠”“欢迎到店”等不含具体数字的文案。\n"
                "底图要求：只生成场景、菜品、桌面、光影和装饰，绝对不要生成任何文字、字母、"
                "数字、Logo、二维码、水印或标签。为后续独立文字层留出合适的深色/干净区域，"
                "但不要画出版式框线。\n"
                "background_prompt 只写本张海报的场景、菜品、光线和构图，"
                "不要写文字约束、画面比例或品牌通用规则。\n"
                "优先级：用户明确提出的风格、颜色或构图要求高于品牌规范；用户未指定时才完整遵循品牌规范。\n"
                f"用户需求：{brief or '做一张有高级感的餐饮促销海报'}\n"
                f"是否提供 Logo：{'是' if has_logo else '否'}\n"
                f"品牌提示：{brand_hint or '未提供'}\n"
                "只输出 JSON，结构：\n"
                '{"creative_direction":"...","background_prompt":"...","copy":'
                '{"brand":"...","headline":"...","subheadline":"...","offer":"...",'
                '"price":"...","details":"...","cta":"...","legal":"..."}}\n'
                "没有的文案字段省略，不要输出空字符串。"
            ),
        }
    ]
    for path in product_paths[:4]:
        content.append(
            {"type": "image_url", "image_url": {"url": _image_data_url(path, 640)}}
        )
    if brand and brand.reference_paths:
        content.append(
            {
                "type": "text",
                "text": (
                    "以下图片是品牌风格参考，只学习配色、构图、字体气质和装饰语言；"
                    "不要复制其中的文字、价格、日期、人物、Logo 或具体版式。"
                ),
            }
        )
        for path in brand.reference_paths[:3]:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _image_data_url(path, 512)},
                }
            )
    messages = [
        {
            "role": "system",
            "content": "你只输出一个合法 JSON 对象，不要 Markdown、解释或代码块。",
        },
        {"role": "user", "content": content},
    ]
    raw = client.chat_json(messages, model=settings.toapis_design_model)
    copy = _clean_copy(raw.get("copy") if isinstance(raw.get("copy"), dict) else {})
    background_prompt = str(raw.get("background_prompt") or "").strip()
    if len(background_prompt) < 20:
        background_prompt = build_background_prompt(
            brief, has_logo=has_logo, product_count=len(product_paths)
        )
    return {
        "creative_direction": str(raw.get("creative_direction") or layout_guide)[:500],
        "background_prompt": background_prompt[:2000],
        "copy": copy,
        "source": "model",
    }


def build_background_prompt(
    brief: str,
    *,
    has_logo: bool,
    product_count: int,
    brand: BrandStyle | None = None,
) -> str:
    if product_count > 1:
        base = (
            f"参考图包含 {product_count} 张商品/门店素材，请融合成同一张商业海报底图。"
            "保留每张图中菜品、商品或门店的主体特征、数量、摆盘和真实质感，"
            "参考图1作为主视觉，不要遗漏任何一张。"
        )
    else:
        base = (
            "以参考图中的菜品/商品为绝对主体，保留其外观、数量、摆盘、质感和真实细节。"
        )
    creative = (
        f"用户需求：{brief}。"
        if brief
        else "设计一张高端火锅/中餐促销海报的底图，暖色灯光，红绿品牌氛围，画面有食欲。"
    )
    background_guide = brand.background_guide if brand else ""
    return (
        f"{base}{creative}"
        + (f"品牌摄影与场景规范：\n{background_guide}\n" if background_guide else "")
        + ("优先级：用户明确提出的背景、风格或颜色要求高于品牌摄影规范；"
           "用户未指定时才完整遵循品牌规范。\n" if background_guide else "")
        + "商业社媒海报构图，菜品大图、层次丰富、色彩鲜明、有促销氛围。"
        "画面中不要出现任何文字、字母、数字、价格、Logo、二维码、水印、标签或招牌字样；"
        "不要画文字占位框。"
        "在画面上方和下方留出相对干净、有明暗对比的区域，方便后期叠加超大标题和价格。"
        + ("右上角保留干净区域用于放置品牌 Logo。" if has_logo else "")
    )


def make_layout(
    settings: Settings,
    client: ToAPIsClient,
    *,
    background_path: Path,
    canvas: dict[str, int],
    copy: dict[str, str],
    has_logo: bool,
    brand: BrandStyle | None = None,
) -> dict[str, Any]:
    layout: dict[str, Any]
    if settings.design_model_enabled and client is not None:
        try:
            layout = _llm_layout(
                settings,
                client,
                background_path=background_path,
                canvas=canvas,
                copy=copy,
                has_logo=has_logo,
                brand=brand,
            )
        except Exception as exc:  # noqa: BLE001 - fallback keeps editing usable
            logger.warning("layout model failed: %s", exc)
            layout = fallback_layout(canvas, copy, has_logo=has_logo, brand=brand)
    else:
        layout = fallback_layout(canvas, copy, has_logo=has_logo, brand=brand)
    layout = repair_layout(layout, canvas, copy, has_logo=has_logo, brand=brand)
    _resolve_logo_overlap(layout, canvas)
    return layout


def promo_layout(
    canvas: dict[str, int],
    copy: dict[str, str],
    *,
    has_logo: bool,
    brand: BrandStyle | None = None,
) -> dict[str, Any]:
    """A deliberately ad-like composition used when the planner clumps text.

    It keeps the Agent's copy and color intent but places the hierarchy across
    the whole canvas: oversized headline at the top, offer badge, a price
    ribbon at the bottom, a CTA tag and small legal/details copy.
    """
    width, height = int(canvas["width"]), int(canvas["height"])
    margin = int(width * 0.035)
    layers: list[dict[str, Any]] = []
    palette = brand.palette if brand else DEFAULT_PALETTE
    fonts = brand.fonts if brand else DEFAULT_FONTS

    def add(
        role: str,
        box: dict[str, int],
        *,
        font_size: float,
        font_weight: int,
        color: str,
        align: str = "left",
        rotation: float = 0.0,
        stroke: dict[str, Any] | None = None,
        shadow: dict[str, Any] | None = None,
        background: dict[str, Any] | None = None,
        letter_spacing: int = 0,
        line_height: float = 1.05,
        font_family: str | None = None,
    ) -> None:
        if role not in copy or not copy[role].strip():
            return
        layers.append(
            {
                "role": role,
                "content": copy[role],
                "box": box,
                "font_size": font_size,
                "font_weight": font_weight,
                "color": color,
                "align": align,
                "vertical_align": "top",
                "line_height": line_height,
                "letter_spacing": letter_spacing,
                "font_family": font_family or fonts.get("body", "sans"),
                "rotation": rotation,
                "stroke": stroke or {"width": 0, "color": "#000000"},
                "shadow": shadow
                or {"blur": 0, "offset_x": 0, "offset_y": 0, "color": "#00000000"},
                "background": background,
            }
        )

    add(
        "brand",
        {
            "x": margin,
            "y": int(height * 0.025),
            "width": int(width * 0.42),
            "height": int(height * 0.055),
        },
        font_size=int(width * 0.027),
        font_weight=700,
        color=palette.get("highlight", "#FFE36B"),
        letter_spacing=2,
        stroke={
            "width": max(1, int(width * 0.002)),
            "color": palette.get("accent_deep", "#5B1B12"),
        },
        font_family=fonts.get("display", "brush"),
    )
    add(
        "headline",
        {
            "x": margin,
            "y": int(height * 0.075),
            "width": int(width * 0.78),
            "height": int(height * 0.20),
        },
        font_size=min(int(width * 0.19), int(height * 0.22)),
        font_weight=900,
        color=palette.get("cream", "#FFF4DF"),
        rotation=-2,
        stroke={
            "width": max(3, int(width * 0.006)),
            "color": palette.get("accent", "#A51F19"),
        },
        shadow={
            "blur": max(6, int(width * 0.012)),
            "offset_x": 0,
            "offset_y": max(3, int(width * 0.004)),
            "color": "#00000099",
        },
        line_height=0.98,
        font_family=fonts.get("display", "brush"),
    )
    add(
        "subheadline",
        {
            "x": margin,
            "y": int(height * 0.285),
            "width": int(width * 0.66),
            "height": int(height * 0.06),
        },
        font_size=int(width * 0.034),
        font_weight=700,
        color=palette.get("highlight", "#FFE36B"),
        stroke={
            "width": max(1, int(width * 0.002)),
            "color": palette.get("accent_deep", "#6B130D"),
        },
    )
    add(
        "offer",
        {
            "x": margin,
            "y": int(height * 0.375),
            "width": int(width * 0.54),
            "height": int(height * 0.08),
        },
        font_size=int(width * 0.034),
        font_weight=900,
        color=palette.get("white", "#FFFFFF"),
        rotation=-3,
        background={
            "color": f"{palette.get('accent_bright', '#D92D20')}FF",
            "radius": max(14, int(width * 0.02)),
            "padding": max(10, int(width * 0.014)),
        },
        stroke={
            "width": max(1, int(width * 0.002)),
            "color": palette.get("accent_deep", "#7A160F"),
        },
    )
    add(
        "price",
        {
            "x": margin,
            "y": int(height * 0.64),
            "width": int(width * 0.80),
            "height": int(height * 0.14),
        },
        font_size=min(int(width * 0.10), int(height * 0.14)),
        font_weight=900,
        color=palette.get("highlight", "#FFE36B"),
        rotation=-2,
        background={
            "color": f"{palette.get('accent', '#C92F1F')}FF",
            "radius": max(8, int(width * 0.01)),
            "padding": max(12, int(width * 0.018)),
        },
        stroke={
            "width": max(3, int(width * 0.005)),
            "color": palette.get("accent_deep", "#8F1712"),
        },
        shadow={
            "blur": max(5, int(width * 0.008)),
            "offset_x": 0,
            "offset_y": max(2, int(width * 0.003)),
            "color": "#00000088",
        },
        line_height=1.0,
        font_family=fonts.get("price", "sans"),
    )
    add(
        "cta",
        {
            "x": int(width * 0.66),
            "y": int(height * 0.805),
            "width": int(width * 0.30),
            "height": int(height * 0.065),
        },
        font_size=int(width * 0.032),
        font_weight=900,
        color=palette.get("cta_text", "#153F2A"),
        align="center",
        rotation=3,
        background={
            "color": f"{palette.get('cream', '#FFF4DF')}FF",
            "radius": max(14, int(width * 0.02)),
            "padding": max(8, int(width * 0.012)),
        },
        stroke={"width": 0, "color": "#000000"},
    )
    add(
        "details",
        {
            "x": margin,
            "y": int(height * 0.90),
            "width": int(width * 0.86),
            "height": int(height * 0.085),
        },
        font_size=int(width * 0.021),
        font_weight=400,
        color=palette.get("cream", "#FFF4DC"),
        line_height=1.35,
        stroke={"width": 1, "color": palette.get("accent_deep", "#5B1B12")},
    )

    logo_box = None
    if has_logo:
        logo_size = int(width * 0.11)
        logo_box = {
            "x": width - margin - logo_size,
            "y": int(height * 0.025),
            "width": logo_size,
            "height": logo_size,
        }
    return {
        "layers": layers,
        "logo_box": logo_box,
        "notes": "promo reflow: full-canvas hierarchy",
        "source": "promo-reflow",
        "brand_id": brand.id if brand else None,
    }


def repair_layout(
    layout: dict[str, Any],
    canvas: dict[str, int],
    copy: dict[str, str],
    *,
    has_logo: bool,
    brand: BrandStyle | None = None,
) -> dict[str, Any]:
    """Reject top-left clumps / PPT-like stacks and enforce ad hierarchy."""
    items = [item for item in layout.get("layers", []) if isinstance(item, dict)]
    boxes = [
        item.get("box")
        for item in items
        if isinstance(item.get("box"), dict)
    ]
    if not boxes:
        return promo_layout(canvas, copy, has_logo=has_logo, brand=brand)
    max_bottom = max(int(box.get("y", 0)) + int(box.get("height", 0)) for box in boxes)
    price = next((item for item in items if item.get("role") == "price"), None)
    price_box = price.get("box") if isinstance(price, dict) else None
    price_y = int(price_box.get("y", 0)) if isinstance(price_box, dict) else 0
    height = int(canvas["height"])
    if max_bottom < int(height * 0.70) or price_y < int(height * 0.55):
        return promo_layout(canvas, copy, has_logo=has_logo, brand=brand)

    # Keep a good model layout, but make sure the key promotional elements read
    # as advertising rather than a plain form.
    width = int(canvas["width"])
    palette = brand.palette if brand else DEFAULT_PALETTE
    fonts = brand.fonts if brand else DEFAULT_FONTS
    for item in items:
        role = item.get("role")
        family = (
            fonts.get("display", "brush")
            if role in {"headline", "brand"}
            else fonts.get("price", "sans")
            if role == "price"
            else fonts.get("body", "sans")
        )
        if item.get("font_family") not in {"sans", "brush"}:
            item["font_family"] = family
        if "rotation" not in item:
            item["rotation"] = -3 if role in {"offer", "price"} else (3 if role == "cta" else 0)
        if role == "headline":
            item["font_size"] = max(
                int(item.get("font_size") or 0),
                min(int(width * 0.16), int(height * 0.20)),
            )
            item["font_weight"] = 900
            item.setdefault(
                "stroke",
                {
                    "width": max(3, int(width * 0.005)),
                    "color": palette.get("accent", "#A51F19"),
                },
            )
            item.setdefault(
                "shadow",
                {
                    "blur": max(6, int(width * 0.01)),
                    "offset_x": 0,
                    "offset_y": max(3, int(width * 0.004)),
                    "color": "#00000099",
                },
            )
        if role == "offer" and not item.get("background"):
            item["background"] = {
                "color": f"{palette.get('accent_bright', '#D92D20')}FF",
                "radius": max(14, int(width * 0.02)),
                "padding": max(10, int(width * 0.014)),
            }
            item["color"] = palette.get("white", "#FFFFFF")
        if role == "price":
            if not item.get("background"):
                item["background"] = {
                    "color": f"{palette.get('accent', '#C92F1F')}FF",
                    "radius": max(8, int(width * 0.01)),
                    "padding": max(12, int(width * 0.018)),
                }
            item["font_size"] = max(
                int(item.get("font_size") or 0),
                min(int(width * 0.09), int(height * 0.13)),
            )
            item["font_weight"] = 900
            item["color"] = item.get("color") or palette.get("highlight", "#FFE36B")
        if role == "cta" and not item.get("background"):
            item["background"] = {
                "color": f"{palette.get('cream', '#FFF4DF')}FF",
                "radius": max(14, int(width * 0.02)),
                "padding": max(8, int(width * 0.012)),
            }
            item["color"] = palette.get("cta_text", "#153F2A")
    return layout


def _resolve_logo_overlap(layout: dict[str, Any], canvas: dict[str, int]) -> None:
    """Keep the logo clear of text boxes without moving any text content.

    The layout model occasionally places a huge headline over the logo. Since the
    logo is baked into the background before text is rendered, an overlap hides
    part of the brand mark. We prefer shrinking the text box (renderer then
    auto-fits) and only move it down when there is not enough width left.
    """
    logo = layout.get("logo_box")
    if not isinstance(logo, dict):
        return
    gap = max(16, int(canvas["width"] * 0.018))
    logo_left = int(logo["x"])
    logo_top = int(logo["y"])
    logo_right = logo_left + int(logo["width"])
    logo_bottom = logo_top + int(logo["height"])
    for item in layout.get("layers", []):
        box = item.get("box")
        if not isinstance(box, dict):
            continue
        left = int(box["x"])
        top = int(box["y"])
        right = left + int(box["width"])
        bottom = top + int(box["height"])
        horizontal = left < logo_right and right > logo_left
        vertical = top < logo_bottom and bottom > logo_top
        if not (horizontal and vertical):
            continue
        if left < logo_left:
            new_width = logo_left - gap - left
            if new_width >= int(canvas["width"] * 0.18):
                box["width"] = new_width
                continue
        elif left >= logo_left:
            new_width = logo_left - gap - left
            if new_width >= int(canvas["width"] * 0.18):
                box["width"] = new_width
                continue
        new_top = logo_bottom + gap
        if new_top + int(box["height"]) <= canvas["height"]:
            box["y"] = new_top
        else:
            # Last resort: keep the text readable and move it left of the logo.
            box["width"] = max(120, logo_left - gap - left)


def fit_logo_box(
    layout: dict[str, Any],
    canvas: dict[str, int],
    *,
    logo_aspect: float,
) -> None:
    """Preserve the original logo aspect ratio instead of squashing it."""
    box = layout.get("logo_box")
    if not isinstance(box, dict) or logo_aspect <= 0:
        return
    width = int(box.get("width") or 0)
    height = int(box.get("height") or 0)
    if width <= 0 or height <= 0:
        return
    # Use the smaller of the requested dimensions as the binding dimension so
    # the logo never grows beyond the layout's reserved area.
    if width / height > logo_aspect:
        width = max(1, int(height * logo_aspect))
    else:
        height = max(1, int(width / logo_aspect))
    max_width = int(canvas["width"] * 0.24)
    if width > max_width:
        width = max_width
        height = max(1, int(width / logo_aspect))
    max_height = int(canvas["height"] * 0.16)
    if height > max_height:
        height = max_height
        width = max(1, int(height * logo_aspect))
    box["width"] = width
    box["height"] = height
    box["x"] = int(min(max(0, box.get("x", 0)), canvas["width"] - width))
    box["y"] = int(min(max(0, box.get("y", 0)), canvas["height"] - height))


def _llm_layout(
    settings: Settings,
    client: ToAPIsClient,
    *,
    background_path: Path,
    canvas: dict[str, int],
    copy: dict[str, str],
    has_logo: bool,
    brand: BrandStyle | None = None,
) -> dict[str, Any]:
    layout_guide = brand.layout_guide if brand else STYLE_GUIDE
    palette = brand.palette if brand else DEFAULT_PALETTE
    fonts = brand.fonts if brand else DEFAULT_FONTS
    brand_name = brand.name if brand else "未指定品牌"
    payload = {
        "canvas": canvas,
        "copy": copy,
        "has_logo": has_logo,
        "brand": brand_name,
        "palette": palette,
        "fonts": fonts,
    }
    text = (
        "你是资深商业海报排版师。根据这张已经生成好的底图，为中文促销海报安排独立文字层。\n"
        f"品牌：{brand_name}\n"
        f"品牌排版规范：\n{layout_guide}\n"
        f"品牌色板：{json.dumps(palette, ensure_ascii=False)}\n"
        f"字体映射：主标题/品牌名使用 {fonts.get('display', 'brush')}，"
        f"正文/活动信息使用 {fonts.get('body', 'sans')}，价格使用 {fonts.get('price', 'sans')}。\n"
        "要求：\n"
        "1. 主标题必须最大、最醒目，使用 display 字体、描边或阴影；文字不能遮挡菜品主体。\n"
        "2. 价格/优惠信息要有强对比，使用品牌红、暖黄或深绿。\n"
        "3. 只使用给定 copy 的原文，绝对不要改写、增删或翻译文案。\n"
        "4. 每个非空 copy 字段生成一个独立 text layer；box 为画布像素坐标。\n"
        "5. 文字必须可读，避免与 Logo 区域重叠；不要给文字加不透明大底板。\n"
        "6. 必须用满画布纵向空间：主标题在上部，促销/价格在下部，价格不要堆在画布上半部；"
        "不要把所有文字挤在左上角。\n"
        "7. 广告感：不同层级字号差异要明显；价格/优惠/CTA 可使用带品牌色的倾斜标签"
        "（background + rotation，rotation 取 -6 到 6 度），主标题可使用轻微旋转。\n"
        "8. 如果有 Logo，给出右上角或视觉平衡处的 logo_box，尺寸约为画布宽 10-16%。\n"
        "9. 每个 layer 必须给出 font_family，只能取 brush 或 sans，并按字体映射选择。\n"
        "10. 用户明确提出的风格要求高于品牌规范；未指定时遵循品牌规范。\n"
        "只输出 JSON：\n"
        '{"layers":[{"role":"headline","box":{"x":0,"y":0,"width":0,"height":0},'
        '"font_size":120,"font_weight":900,"color":"#FFFFFF","align":"left",'
        '"font_family":"brush",'
        '"vertical_align":"top","line_height":1.05,"letter_spacing":0,'
        '"rotation":0,'
        '"stroke":{"width":6,"color":"#7A1B12"},'
        '"shadow":{"blur":12,"offset_x":0,"offset_y":6,"color":"#00000088"},'
        '"background":null}],"logo_box":null,"notes":"..."}\n'
        f"数据：{json.dumps(payload, ensure_ascii=False)}"
    )
    user_content: list[dict[str, Any]] = [
        {"type": "text", "text": text},
        {
            "type": "image_url",
            "image_url": {"url": _image_data_url(background_path, 900)},
        },
    ]
    if brand and brand.reference_paths:
        user_content.append(
            {
                "type": "text",
                "text": (
                    "以下图片是品牌排版参考，只学习构图、层级、字体气质和装饰语言；"
                    "不要复制其中的文字、价格、日期、人物或 Logo。"
                ),
            }
        )
        for path in brand.reference_paths[:3]:
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _image_data_url(path, 512)},
                }
            )
    messages = [
        {
            "role": "system",
            "content": "你只输出一个合法 JSON 对象，不要 Markdown、解释或代码块。",
        },
        {"role": "user", "content": user_content},
    ]
    raw = client.chat_json(messages, model=settings.toapis_design_model)
    layers = raw.get("layers")
    if not isinstance(layers, list):
        raise ToAPIsError("design_invalid_layout", "排版模型没有返回 layers")
    seen_roles: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in layers:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        if role not in ROLE_NAMES or role not in copy or role in seen_roles:
            continue
        seen_roles.add(role)
        normalized.append(
            {
                "role": role,
                "content": copy[role],
                "box": _clamp_box(item.get("box"), canvas),
                "font_size": item.get("font_size", 72),
                "font_weight": item.get("font_weight", 700),
                "font_family": (
                    item.get("font_family")
                    if item.get("font_family") in {"sans", "brush"}
                    else None
                ),
                "color": item.get("color"),
                "align": item.get("align"),
                "vertical_align": item.get("vertical_align"),
                "line_height": item.get("line_height"),
                "letter_spacing": item.get("letter_spacing"),
                "stroke": item.get("stroke"),
                "shadow": item.get("shadow"),
                "background": item.get("background"),
            }
        )
    if not any(item["role"] == "headline" for item in normalized):
        raise ToAPIsError("design_invalid_layout", "排版模型漏掉了主标题")

    logo_box = raw.get("logo_box") if has_logo else None
    return {
        "layers": normalized,
        "logo_box": (
            _clamp_box(
                logo_box,
                canvas,
                default={
                    "x": int(canvas["width"] * 0.84),
                    "y": int(canvas["height"] * 0.04),
                    "width": int(canvas["width"] * 0.12),
                    "height": int(canvas["width"] * 0.12),
                },
            )
            if has_logo
            else None
        ),
        "notes": str(raw.get("notes") or "")[:300],
        "source": "model",
    }


def fallback_layout(
    canvas: dict[str, int],
    copy: dict[str, str],
    *,
    has_logo: bool,
    brand: BrandStyle | None = None,
) -> dict[str, Any]:
    width, height = canvas["width"], canvas["height"]
    margin = int(width * 0.06)
    layers: list[dict[str, Any]] = []
    palette = brand.palette if brand else DEFAULT_PALETTE
    fonts = brand.fonts if brand else DEFAULT_FONTS

    if "brand" in copy:
        layers.append(
            {
                "role": "brand",
                "box": {"x": margin, "y": int(height * 0.045), "width": int(width * 0.45), "height": int(height * 0.055)},
                "font_size": max(24, int(width * 0.032)),
                "font_weight": 700,
                "font_family": fonts.get("display", "brush"),
                "color": palette.get("cream", "#FFF6E8"),
                "align": "left",
                "letter_spacing": 2,
            }
        )
    if "offer" in copy:
        layers.append(
            {
                "role": "offer",
                "box": {"x": margin, "y": int(height * 0.12), "width": int(width * 0.48), "height": int(height * 0.075)},
                "font_size": max(26, int(width * 0.038)),
                "font_weight": 900,
                "font_family": fonts.get("body", "sans"),
                "color": palette.get("white", "#FFFFFF"),
                "align": "left",
                "background": {
                    "color": f"{palette.get('accent', '#C92F1F')}FF",
                    "radius": 18,
                    "padding": 14,
                },
                "rotation": 0,
            }
        )
    layers.append(
        {
            "role": "headline",
            "box": {"x": margin, "y": int(height * (0.18 if has_logo else 0.14)), "width": int(width * 0.86), "height": int(height * 0.22)},
            "font_size": min(
                int(width * 0.24),
                max(56, int(width * 0.95 / max(2, min(len(copy.get("headline", "")), 6)))),
            ),
            "font_weight": 900,
            "font_family": fonts.get("display", "brush"),
            "color": palette.get("cream", "#FFFFFF"),
            "align": "left",
            "line_height": 1.02,
            "letter_spacing": 0,
            "stroke": {
                "width": max(2, int(width * 0.005)),
                "color": palette.get("accent_deep", "#7C160F"),
            },
            "shadow": {"blur": 14, "offset_x": 0, "offset_y": 8, "color": "#00000099"},
        }
    )
    if "subheadline" in copy:
        layers.append(
            {
                "role": "subheadline",
                "box": {"x": margin, "y": int(height * 0.41), "width": int(width * 0.7), "height": int(height * 0.1)},
                "font_size": max(24, int(width * 0.036)),
                "font_weight": 500,
                "font_family": fonts.get("body", "sans"),
                "color": palette.get("cream", "#FFF4DF"),
                "align": "left",
                "line_height": 1.2,
                "stroke": {"width": 1, "color": "#00000066"},
                "shadow": {"blur": 8, "offset_x": 0, "offset_y": 4, "color": "#00000088"},
            }
        )
    if "price" in copy:
        layers.append(
            {
                "role": "price",
                "box": {"x": margin, "y": int(height * 0.72), "width": int(width * 0.6), "height": int(height * 0.14)},
                "font_size": max(42, int(width * 0.09)),
                "font_weight": 900,
                "font_family": fonts.get("price", "sans"),
                "color": palette.get("highlight", "#FFD54A"),
                "align": "left",
                "stroke": {
                    "width": max(2, int(width * 0.004)),
                    "color": palette.get("accent_deep", "#6B130D"),
                },
                "shadow": {"blur": 10, "offset_x": 0, "offset_y": 6, "color": "#000000AA"},
            }
        )
    if "details" in copy:
        layers.append(
            {
                "role": "details",
                "box": {"x": margin, "y": int(height * 0.88), "width": int(width * 0.7), "height": int(height * 0.065)},
                "font_size": max(20, int(width * 0.028)),
                "font_weight": 500,
                "font_family": fonts.get("body", "sans"),
                "color": palette.get("cream", "#FFFFFF"),
                "align": "left",
                "line_height": 1.25,
                "shadow": {"blur": 6, "offset_x": 0, "offset_y": 3, "color": "#00000099"},
            }
        )
    if "cta" in copy:
        layers.append(
            {
                "role": "cta",
                "box": {"x": int(width * 0.62), "y": int(height * 0.86), "width": int(width * 0.32), "height": int(height * 0.075)},
                "font_size": max(22, int(width * 0.03)),
                "font_weight": 700,
                "font_family": fonts.get("body", "sans"),
                "color": palette.get("cta_text", "#153F2A"),
                "align": "center",
                "background": {
                    "color": f"{palette.get('cream', '#F7F1DE')}FF",
                    "radius": 22,
                    "padding": 12,
                },
            }
        )
    if "legal" in copy:
        layers.append(
            {
                "role": "legal",
                "box": {"x": margin, "y": int(height * 0.955), "width": int(width * 0.88), "height": int(height * 0.035)},
                "font_size": max(16, int(width * 0.02)),
                "font_weight": 300,
                "font_family": fonts.get("body", "sans"),
                "color": f"{palette.get('cream', '#FFFFFF')}CC",
                "align": "left",
            }
        )

    logo_box = None
    if has_logo:
        size = int(width * 0.12)
        logo_box = {
            "x": width - margin - size,
            "y": int(height * 0.045),
            "width": size,
            "height": size,
        }
    for item in layers:
        item["content"] = copy.get(item["role"], "")
    return {
        "layers": layers,
        "logo_box": logo_box,
        "notes": "deterministic fallback",
        "source": "fallback",
        "brand_id": brand.id if brand else None,
    }


def layout_to_layers(
    layout: dict[str, Any],
    *,
    canvas: dict[str, int],
    logo_asset_id: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    text_layers: list[dict[str, Any]] = []
    z_index = 10
    for item in layout.get("layers", []):
        role = str(item.get("role") or "headline")
        if role not in ROLE_NAMES:
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        style = {
            "font_size": item.get("font_size", 72),
            "font_weight": item.get("font_weight", 700),
            "font_family": item.get("font_family"),
            "color": item.get("color"),
            "align": item.get("align"),
            "vertical_align": item.get("vertical_align"),
            "line_height": item.get("line_height"),
            "letter_spacing": item.get("letter_spacing"),
            "stroke": item.get("stroke"),
            "shadow": item.get("shadow"),
            "background": item.get("background"),
        }
        layer = design_utils.make_text_layer(
            layer_id=f"text_{role}",
            name=ROLE_NAMES[role],
            role=role,
            content=content,
            box=item.get("box") or {},
            style=style,
            z_index=z_index,
            rotation=float(item.get("rotation") or 0),
        )
        text_layers.append(layer)
        z_index += 10

    logo = None
    if logo_asset_id and layout.get("logo_box"):
        logo = design_utils.make_logo_layer(
            layer_id="logo_brand",
            asset_id=logo_asset_id,
            box=layout["logo_box"],
            mode="model_integrated",
            z_index=z_index,
        )
    return text_layers, logo


def brand_hint_from_asset(path: Path | None) -> str | None:
    if path is None:
        return None
    name = path.stem.lower()
    if "seejoy" in name or "喜聚" in path.stem:
        return "SEEJOY 喜聚"
    return None
