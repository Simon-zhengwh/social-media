from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import re
from pathlib import Path
from typing import Any

from PIL import Image

from app.config import Settings
from app.services.brand_style import DEFAULT_FONTS, DEFAULT_PALETTE, BrandStyle
from app.services.core_ai import CoreAIClient, CoreAIError
from app.services.image_ref import ImageRef
from app.services import design as design_utils

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
    "caption": "手写批注",
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
    "caption",
]

ROLE_ALIASES = {
    "title": "headline",
    "main_title": "headline",
    "subtitle": "subheadline",
    "promotion": "offer",
    "promo": "offer",
    "discount": "offer",
    "annotation": "caption",
    "handwritten": "caption",
    "note": "caption",
    "footnote": "legal",
}

STYLE_GUIDE = (
    "目标风格：高质量中式餐饮社媒海报。真实菜品是主体，品牌色鲜明，"
    "标题有表现力，信息层级清晰；构图由本次需求决定，不预设固定的上下分区。"
)

_NUMERIC_PATTERN = re.compile(
    r"(?:[$¥€]\s?\d+(?:[.,]\d+)?|\d+(?:[.,]\d+)?\s?(?:%|折|元|块|人|位|月|日|点|年|岁|pm|am|PM|AM)|\d+(?:[.,]\d+)?)"
)


def _image_data_url(value: Path | ImageRef | str, max_side: int = 768) -> str:
    if isinstance(value, ImageRef):
        if value.url:
            return value.url
        if value.path is None:
            raise CoreAIError("invalid_reference", "参考图没有可用的本地路径或公网地址", 422)
        path = value.path
    elif isinstance(value, str):
        if value.startswith(("http://", "https://")):
            return value
        path = Path(value)
    else:
        path = value
    with Image.open(path) as source:
        image = source.convert("RGB")
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=82)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"



def _brand_reference_images(brand: BrandStyle | None) -> list[Path | ImageRef]:
    if brand is None:
        return []
    if brand.reference_inputs:
        return list(brand.reference_inputs)
    return list(brand.reference_paths)


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise CoreAIError("design_invalid_json", "设计模型没有返回 JSON 对象")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise CoreAIError("design_invalid_json", "设计模型返回的 JSON 无法解析") from exc
    if not isinstance(payload, dict):
        raise CoreAIError("design_invalid_json", "设计模型返回内容不是对象")
    return payload


def _canonical_role(value: Any) -> str:
    role = str(value or "").strip()
    role = ROLE_ALIASES.get(role, role)
    return role if role in ROLE_NAMES else ""


def _numeric_tokens(text: str) -> set[str]:
    return {match.group(0).replace(" ", "").lower() for match in _NUMERIC_PATTERN.finditer(text or "")}


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _split_long_copy(text: str, *, limit: int = 500) -> list[str]:
    text = str(text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    pieces = re.split(r"(?<=[。！？；\n])", text)
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if not piece:
            continue
        if len(current) + len(piece) <= limit:
            current += piece
        else:
            if current:
                chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    if any(len(chunk) > limit for chunk in chunks):
        raise CoreAIError("copy_too_long", "单条业务文案超过 500 字，请拆分为多个文案条目")
    return chunks

def _copy_items_from_raw(raw: dict[str, Any], brief: str) -> list[dict[str, Any]]:
    source_items = raw.get("copy_items")
    items: list[dict[str, Any]] = []
    if isinstance(source_items, list):
        for item in source_items:
            if not isinstance(item, dict):
                continue
            role = _canonical_role(item.get("role"))
            if not role:
                continue
            raw_id = str(item.get("id") or "").strip()
            source = str(item.get("source") or "creative")
            if source not in {"user", "creative"}:
                source = "creative"
            chunks = _split_long_copy(str(item.get("text") or ""))
            for index, chunk in enumerate(chunks):
                copy_id = raw_id or f"copy_{role}_{len(items) + 1}"
                if len(chunks) > 1:
                    copy_id = f"{copy_id}_{index + 1}"
                items.append({
                    "id": copy_id,
                    "role": role,
                    "text": chunk,
                    "source": source,
                    "group_id": item.get("group_id"),
                    "priority": int(item.get("priority") or len(items) + 1),
                })
    else:
        copy = raw.get("copy") if isinstance(raw.get("copy"), dict) else {}
        for role in ROLE_ORDER:
            chunks = _split_long_copy(str(copy.get(role) or ""))
            for index, chunk in enumerate(chunks):
                items.append({
                    "id": f"copy_{role}_{index + 1}",
                    "role": role,
                    "text": chunk,
                    "source": "creative",
                    "group_id": None,
                    "priority": len(items) + 1,
                })
    if not items:
        fallback = _fallback_copy(brief)
        for role, text in fallback.items():
            items.append({
                "id": f"copy_{role}_1",
                "role": role,
                "text": text,
                "source": "creative",
                "group_id": None,
                "priority": len(items) + 1,
            })
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in items:
        copy_id = re.sub(r"[^A-Za-z0-9_-]", "_", item["id"])[:80] or f"copy_{len(normalized) + 1}"
        if copy_id in seen:
            copy_id = f"{copy_id}_{len(normalized) + 1}"
        seen.add(copy_id)
        item["id"] = copy_id
        normalized.append(item)
    brief_numbers = _numeric_tokens(brief)
    for item in normalized:
        invented = _numeric_tokens(item["text"]) - brief_numbers
        if invented:
            raise CoreAIError(
                "copy_fact_violation",
                f"文案包含用户未提供的数字信息：{', '.join(sorted(invented))}",
            )
        if item["source"] == "user" and _normalize_text(item["text"]) not in _normalize_text(brief):
            raise CoreAIError("copy_fact_violation", "用户文案没有在原始需求中原文出现")
    if not any(item["role"] == "headline" for item in normalized):
        normalized.insert(0, {
            "id": "copy_headline_1",
            "role": "headline",
            "text": _fallback_copy(brief)["headline"],
            "source": "creative",
            "group_id": None,
            "priority": 0,
        })
    return normalized


def _fallback_copy(brief: str) -> dict[str, str]:
    quoted = re.findall(r"[“\"「『]([^”\"」』]{2,24})[”\"」』]", brief)
    headline = quoted[0] if quoted else ""
    if not headline:
        for keyword in ("开业", "火锅", "自助", "套餐", "新品", "限时", "特惠", "周末"):
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

def _normalize_design_intent(raw: Any, brand: BrandStyle | None) -> dict[str, Any]:
    intent = design_utils.normalize_design_intent(raw)
    if not intent.get("palette"):
        palette = DEFAULT_PALETTE
        intent["palette"] = [
            palette.get("primary", "#0D4317"),
            palette.get("accent", "#B12310"),
            palette.get("cream", "#FFF4DF"),
        ]
    canonical = json.dumps(intent, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    intent["hash"] = digest
    intent["id"] = f"intent_{digest[:12]}"
    return intent


def _asset_requests(raw: Any, *, has_logo: bool, product_count: int) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            if role not in {"logo", "product", "decoration"}:
                continue
            requests.append({
                "id": str(item.get("id") or f"asset_request_{len(requests) + 1}")[:80],
                "role": role,
                "asset_id": str(item.get("asset_id") or "") or None,
                "purpose": str(item.get("purpose") or "")[:300],
                "transparency_required": bool(item.get("transparency_required")),
                "placement": item.get("placement") if isinstance(item.get("placement"), dict) else None,
            })
    if has_logo and not any(item["role"] == "logo" for item in requests):
        requests.insert(0, {"id": "asset_logo_1", "role": "logo", "asset_id": None, "purpose": "保留品牌识别", "transparency_required": True, "placement": None})
    if product_count and not any(item["role"] == "product" for item in requests):
        requests.append({"id": "asset_product_background", "role": "product", "asset_id": None, "purpose": "作为底图主体", "transparency_required": False, "placement": None})
    return requests


def _clamp_box(raw: Any, canvas: dict[str, int], *, default: dict[str, int] | None = None) -> dict[str, int]:
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
        x = int(max(-width, min(width * 2, float(raw.get("x", fallback["x"])))))
        y = int(max(-height, min(height * 2, float(raw.get("y", fallback["y"])))))
        box_width = int(max(20, min(width * 2, float(raw.get("width", fallback["width"])))))
        box_height = int(max(20, min(height * 2, float(raw.get("height", fallback["height"])))))
    except (TypeError, ValueError):
        return dict(fallback)
    return {"x": x, "y": y, "width": box_width, "height": box_height}


def make_creative_plan(
    settings: Settings,
    client: CoreAIClient | None,
    *,
    brief: str,
    product_paths: list[Path | ImageRef],
    has_logo: bool,
    brand_hint: str | None = None,
    brand: BrandStyle | None = None,
    canvas: dict[str, int] | None = None,
    capabilities: list[str] | None = None,
    logo_asset_id: str | None = None,
    product_asset_ids: list[str] | None = None,
    style_reference_paths: list[Path | ImageRef] | None = None,
) -> dict[str, Any]:
    canvas = canvas or {"width": int(getattr(settings, "canvas_width", 1024)), "height": int(getattr(settings, "canvas_height", 1024))}
    capabilities = capabilities or list(design_utils.SUPPORTED_CAPABILITIES)
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
                canvas=canvas,
                capabilities=capabilities,
                logo_asset_id=logo_asset_id,
                product_asset_ids=product_asset_ids or [],
                style_reference_paths=style_reference_paths or [],
            )
        except Exception as exc:
            logger.warning("creative plan model failed: %s", exc)

    copy = _fallback_copy(brief)
    copy_items = _copy_items_from_raw({"copy": copy}, brief)
    intent = _normalize_design_intent({
        "concept": brand.layout_guide if brand else STYLE_GUIDE,
        "composition": "由需求决定焦点和图文关系，不强制固定上下区域",
        "typography": "清晰、有层级的标题与正文",
        "palette": [],
    }, brand)
    return {
        "design_intent": intent,
        "creative_direction": brand.layout_guide if brand else STYLE_GUIDE,
        "background_prompt": brief.strip() or "高端火锅/中餐餐桌场景，暖色灯光，菜品有食欲。",
        "copy": copy,
        "copy_items": copy_items,
        "asset_requests": _asset_requests([], has_logo=has_logo, product_count=len(product_paths)),
        "initial_scene_plan": {"layers": []},
        "source": "fallback",
    }

def _llm_creative_plan(
    settings: Settings,
    client: CoreAIClient,
    *,
    brief: str,
    product_paths: list[Path | ImageRef],
    has_logo: bool,
    brand_hint: str | None,
    brand: BrandStyle | None,
    canvas: dict[str, int],
    capabilities: list[str],
    logo_asset_id: str | None,
    product_asset_ids: list[str],
    style_reference_paths: list[Path | ImageRef],
) -> dict[str, Any]:
    layout_guide = brand.layout_guide if brand else STYLE_GUIDE
    brand_name = brand.name if brand else "未指定品牌"
    content: list[dict[str, Any]] = [{
        "type": "text",
        "text": (
            "你是资深商业海报美术指导。先构思整张作品，再输出可执行的文字和对象计划。\n"
            f"品牌：{brand_name}\n品牌偏好（不是硬模板）：\n{layout_guide}\n"
            f"画布：{canvas['width']}x{canvas['height']}\n"
            f"可用渲染能力：{json.dumps(capabilities, ensure_ascii=False)}\n"
            "硬约束：\n"
            "1. 只输出 JSON。用户明确给出的文案必须原文保留，不得改写价格、日期、单位、折扣和地址。\n"
            "2. 没有在用户需求中出现的数字信息绝对不能生成。创意标题可以使用不含业务数字的措辞。\n"
            "3. 构图自由：标题可以在侧边、居中、斜向或与主体有意穿插，不要默认固定上标题下价格。\n"
            "4. 背景只包含场景、摄影、光影和装饰，不生成任何文字、Logo、二维码、水印或可读标签。\n"
            "5. copy_items 使用稳定 id，允许同一 role 多条；source 只能是 user 或 creative。角色只使用 headline/subheadline/price/offer/details/cta/brand/legal/caption。\n"
            "5b. 为贴近 SeeJoy 参考风格，尽量输出 2 条 caption 手写批注（不含业务数字），价格/优惠可用 offer；标题可拆成 2 条同 role headline。\n"
            "5c. initial_scene_plan 至少包含 6 个文字对象和 3 个装饰 shape（价格托盘、优惠徽章、手写下划线/箭头/撕纸带），并给出真实 box 或绝对 points。\n"
            "6. asset_requests 只引用给定素材 id 或声明需要复用的商品/Logo；不要求自动抠图。\n"
            "7. initial_scene_plan 在生图前给出物体关系；后续会依据真实背景校准。\n"
            f"用户需求：{brief or '做一张有高级感的餐饮海报'}\n"
            f"是否提供 Logo：{'是' if has_logo else '否'}；Logo asset_id：{logo_asset_id or '无'}\n"
            f"商品素材 asset_ids：{json.dumps(product_asset_ids, ensure_ascii=False)}\n"
            f"品牌提示：{brand_hint or '未提供'}\n"
            "输出 JSON：\n"
            '{"design_intent":{"concept":"","focal_point":"","composition":"","palette":["#RRGGBB"],"typography":"","text_image_relation":"","decor_strategy":"","density":"balanced"},'
            '"copy_items":[{"id":"copy_01","role":"headline","text":"","source":"user"}],'
            '"asset_requests":[{"id":"asset_req_01","role":"logo","asset_id":"","purpose":"","transparency_required":true,"placement":{"x":0,"y":0,"width":100,"height":100}}],'
            '"initial_scene_plan":{"layers":[{"id":"layer_title_01","type":"text","copy_id":"copy_01","role":"headline","box":{"x":0,"y":0,"width":0,"height":0},"font_size":120,"rotation":0,"z_index":100}]},'
            '"background_prompt":"只写场景、光线、构图和材质"}'
        ),
    }]
    for path in product_paths[:4]:
        content.append({"type": "image_url", "image_url": {"url": _image_data_url(path, 640)}})
    if style_reference_paths:
        content.append({
            "type": "text",
            "text": "以下图片是用户提供的风格参考图，仅学习配色、构图、字体气质、材质和氛围；不要复制其中可读文字、价格、日期、人物或具体版式。",
        })
        for path in style_reference_paths[:3]:
            content.append({"type": "image_url", "image_url": {"url": _image_data_url(path, 640)}})
    brand_references = _brand_reference_images(brand)
    if brand_references:
        content.append({"type": "text", "text": "以下图片是品牌风格参考，只学习配色、构图、字体气质和装饰语言；不要复制其中的文字、价格、日期、人物、Logo 或具体版式。"})
        for path in brand_references[:3]:
            content.append({"type": "image_url", "image_url": {"url": _image_data_url(path, 512)}})
    messages = [
        {"role": "system", "content": "你只输出一个合法 JSON 对象，不要 Markdown、解释或代码块。"},
        {"role": "user", "content": content},
    ]
    raw = client.chat_json(messages, model=settings.core_ai_design_model)
    copy_items = _copy_items_from_raw(raw, brief)
    intent = _normalize_design_intent(raw.get("design_intent"), brand)
    background_prompt = str(raw.get("background_prompt") or "").strip()
    if len(background_prompt) < 20:
        background_prompt = build_background_prompt(brief, has_logo=has_logo, product_count=len(product_paths), brand=brand, design_intent=intent)
    return {
        "design_intent": intent,
        "creative_direction": str(raw.get("creative_direction") or intent.get("concept") or layout_guide)[:500],
        "background_prompt": background_prompt[:2000],
        "copy": {item["role"]: item["text"] for item in copy_items},
        "copy_items": copy_items,
        "asset_requests": _asset_requests(raw.get("asset_requests"), has_logo=has_logo, product_count=len(product_paths)),
        "initial_scene_plan": raw.get("initial_scene_plan") if isinstance(raw.get("initial_scene_plan"), dict) else {"layers": []},
        "source": "model",
    }


def build_background_prompt(
    brief: str,
    *,
    has_logo: bool,
    product_count: int,
    brand: BrandStyle | None = None,
    ratio: str | None = None,
    design_intent: dict[str, Any] | None = None,
    independent_products: bool = False,
) -> str:
    if product_count > 1 and not independent_products:
        base = f"参考图包含 {product_count} 张商品/门店素材，请融合成同一张商业海报底图。保留每张图中菜品、商品或门店的主体特征、数量、摆盘和真实质感，请根据构图需要自由安排各张素材的主次与层次，不要遗漏任何一张。"
    elif independent_products or product_count == 0:
        base = "创建一张高质量商业摄影场景底图，为后续独立商品和文字对象留出自然空间。"
    else:
        base = "以参考图中的菜品/商品为绝对主体，保留其外观、数量、摆盘、质感和真实细节。"
    creative = f"用户需求：{brief}。" if brief else "设计一张高端火锅/中餐海报底图，暖色灯光，红绿品牌氛围，画面有食欲。"
    intent = design_utils.normalize_design_intent(design_intent)
    relation = (
        f"本次构图方向：{intent['composition']}；焦点：{intent['focal_point']}；"
        f"图文关系：{intent['text_image_relation']}；装饰策略：{intent['decor_strategy']}。"
    )
    background_guide = brand.background_guide if brand else ""
    return (
        f"{base}{creative}{relation}"
        + (f"品牌摄影与场景规范：\n{background_guide}\n" if background_guide else "")
        + ("优先级：用户明确提出的背景、风格或颜色要求高于品牌摄影规范；用户未指定时才完整遵循品牌规范。\n" if background_guide else "")
        + (f"画面比例为 {ratio}，按该比例组织主体、留白和视觉重心。\n" if ratio else "")
        + "商业社媒海报构图，层次丰富、色彩鲜明、有真实摄影质感。"
        "画面中不要出现任何文字、字母、数字、价格、Logo、二维码、水印、标签或招牌字样；不要画文字占位框。"
        "按本次设计意图为独立文字和 Logo 留出可读空间，不强制固定在画面上方或下方。"
        + ("为独立 Logo 在右上角或视觉平衡处保留干净区域。" if has_logo else "")
    )

def _copy_lookup(copy_items: list[dict[str, Any]], copy: dict[str, str] | None) -> dict[str, dict[str, Any]]:
    lookup = {str(item.get("id")): item for item in copy_items if item.get("id")}
    if not lookup and copy:
        for index, (role, text) in enumerate(copy.items()):
            lookup[f"copy_{role}_{index + 1}"] = {"id": f"copy_{role}_{index + 1}", "role": role, "text": str(text), "source": "creative"}
    return lookup


def _normalize_layout_layers(
    layers: Any,
    *,
    canvas: dict[str, int],
    copy_items: list[dict[str, Any]],
    copy: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(layers, list):
        return []
    lookup = _copy_lookup(copy_items, copy)
    by_role: dict[str, list[dict[str, Any]]] = {}
    for item in lookup.values():
        by_role.setdefault(str(item.get("role")), []).append(item)
    normalized: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    used_copy_ids: set[str] = set()
    role_counts: dict[str, int] = {}
    for index, item in enumerate(layers):
        if not isinstance(item, dict):
            continue
        role = _canonical_role(item.get("role"))
        if not role:
            continue
        copy_id = str(item.get("copy_id") or "").strip()
        copy_item = lookup.get(copy_id)
        if copy_item is None:
            candidates = by_role.get(role) or []
            copy_item = candidates[min(role_counts.get(role, 0), max(0, len(candidates) - 1))] if candidates else None
        if copy_item is None:
            continue
        copy_key = str(copy_item.get("id") or "")
        if copy_key and copy_key in used_copy_ids:
            continue
        if copy_key:
            used_copy_ids.add(copy_key)
        content = str(copy_item.get("text") or "").strip()
        if not content:
            continue
        role_counts[role] = role_counts.get(role, 0) + 1
        layer_id = str(item.get("id") or item.get("layer_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,80}", layer_id):
            layer_id = f"layer_text_{copy_item['id']}_{role_counts[role]}"
        while layer_id in used_ids:
            layer_id = f"{layer_id}_{len(normalized) + 1}"
        used_ids.add(layer_id)
        box = _clamp_box(item.get("box"), canvas)
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
        normalized.append({
            "id": layer_id,
            "copy_id": copy_item["id"],
            "role": role,
            "content": content,
            "source": copy_item.get("source") or "creative",
            "box": box,
            "font_size": style["font_size"],
            "font_weight": style["font_weight"],
            "font_family": style["font_family"],
            "color": style["color"],
            "align": style["align"],
            "vertical_align": style["vertical_align"],
            "line_height": style["line_height"],
            "letter_spacing": style["letter_spacing"],
            "stroke": style["stroke"],
            "shadow": style["shadow"],
            "background": style["background"],
            "rotation": float(item.get("rotation") or 0),
            "z_index": int(item.get("z_index") or (index + 1) * 10),
        })
    return normalized


def make_layout(
    settings: Settings,
    client: CoreAIClient | None,
    *,
    background_path: Path | ImageRef,
    canvas: dict[str, int],
    copy: dict[str, str] | None = None,
    has_logo: bool,
    brand: BrandStyle | None = None,
    design_intent: dict[str, Any] | None = None,
    copy_items: list[dict[str, Any]] | None = None,
    initial_scene_plan: dict[str, Any] | None = None,
    capabilities: list[str] | None = None,
    fixed_layers: list[dict[str, Any]] | None = None,
    reference_path: Path | ImageRef | None = None,
) -> dict[str, Any]:
    copy_items = copy_items or [{"id": f"copy_{role}_{index + 1}", "role": role, "text": text, "source": "creative"} for index, (role, text) in enumerate((copy or {}).items())]
    initial_layers = _normalize_layout_layers((initial_scene_plan or {}).get("layers") if isinstance(initial_scene_plan, dict) else None, canvas=canvas, copy_items=copy_items, copy=copy)
    layout: dict[str, Any]
    if settings.design_model_enabled and client is not None:
        try:
            layout = _llm_layout(
                settings,
                client,
                background_path=background_path,
                canvas=canvas,
                copy_items=copy_items,
                copy=copy,
                has_logo=has_logo,
                brand=brand,
                design_intent=design_intent,
                initial_layers=initial_layers,
                capabilities=capabilities or list(design_utils.SUPPORTED_CAPABILITIES),
                fixed_layers=fixed_layers or [],
                reference_path=reference_path,
            )
            layout["layers"] = _normalize_layout_layers(layout.get("layers"), canvas=canvas, copy_items=copy_items, copy=copy)
        except Exception as exc:
            logger.warning("layout model failed: %s", exc)
            layout = {"layers": initial_layers, "logo_box": None, "notes": "initial scene plan", "source": "initial"}
    else:
        layout = {"layers": initial_layers, "logo_box": None, "notes": "initial scene plan", "source": "initial"}
    if initial_layers:
        provided = {str(item.get("copy_id") or "") for item in layout.get("layers", [])}
        for item in initial_layers:
            if str(item.get("copy_id") or "") not in provided:
                layout.setdefault("layers", []).append(item)
    if not layout.get("layers"):
        layout = fallback_layout(canvas, copy or {item["role"]: item["text"] for item in copy_items}, has_logo=has_logo, brand=brand, copy_items=copy_items)
    layout = repair_layout(layout, canvas, copy_items=copy_items, has_logo=has_logo, brand=brand)
    return layout

def _llm_layout(
    settings: Settings,
    client: CoreAIClient,
    *,
    background_path: Path | ImageRef,
    canvas: dict[str, int],
    copy_items: list[dict[str, Any]],
    copy: dict[str, str] | None,
    has_logo: bool,
    brand: BrandStyle | None = None,
    design_intent: dict[str, Any] | None = None,
    initial_layers: list[dict[str, Any]] | None = None,
    capabilities: list[str] | None = None,
    fixed_layers: list[dict[str, Any]] | None = None,
    reference_path: Path | ImageRef | None = None,
) -> dict[str, Any]:
    layout_guide = brand.layout_guide if brand else STYLE_GUIDE
    intent = design_utils.normalize_design_intent(design_intent)
    payload = {
        "canvas": canvas,
        "copy_items": copy_items,
        "has_logo": has_logo,
        "brand": brand.name if brand else "未指定品牌",
        "design_intent": intent,
        "initial_layers": initial_layers or [],
        "fixed_layers": fixed_layers or [],
        "capabilities": capabilities or list(design_utils.SUPPORTED_CAPABILITIES),
        "has_global_reference": reference_path is not None,
    }
    text = (
        "你是资深商业海报排版师。根据已经生成的真实底图，校准本次 DesignIntent 的初始对象计划。\n"
        f"品牌：{payload['brand']}\n品牌排版偏好（示例，不是强制模板）：\n{layout_guide}\n"
        "要求：\n"
        "1. 只使用 copy_items 中的原文，通过 copy_id 引用；绝对不要改写、增加、删除或翻译文案。\n"
        "2. 允许同一 role 出现多个文字图层；layer id 必须稳定、唯一。\n"
        "3. 构图自由。标题可以侧置、居中、斜向或与主体有意穿插；不要因为价格不在下半部就重排。\n"
        "4. 依据真实底图调整位置、字号、颜色、描边、阴影、底板和 rotation；rotation 是顺时针角度。\n"
        "5. 文字必须可读；避免意外遮挡关键业务信息，但允许有意图文穿插和出血。\n"
        "6. 如果存在 Logo/商品固定图层，避开其关键可视区域；不要复制这些对象。\n"
        "7. 只输出已声明的渲染能力，不输出 group/filter/mask/渐变。\n"
        "8. 如果附带全局设计参考稿，以它校准标题、价格、优惠贴片的整体位置和层级；但文字内容必须以 copy_items 为准，不复制参考稿里的错字。\n"
        "输出 JSON：\n"
        '{"layers":[{"id":"layer_title_01","copy_id":"copy_01","role":"headline",'
        '"box":{"x":0,"y":0,"width":0,"height":0},"font_size":120,"font_weight":900,'
        '"font_family":"brush","color":"#FFFFFF","align":"left","vertical_align":"top",'
        '"line_height":1.05,"letter_spacing":0,"rotation":0,"z_index":100,'
        '"stroke":{"width":6,"color":"#7A1B12"},"shadow":{"blur":12,"offset_x":0,"offset_y":6,"color":"#00000088"},"background":null}],'
        '"logo_box":null,"notes":"..."}\n'
        f"数据：{json.dumps(payload, ensure_ascii=False)}"
    )
    user_content: list[dict[str, Any]] = [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": _image_data_url(background_path, 900)}},
    ]
    if reference_path is not None:
        user_content.append({"type": "text", "text": "以下图片是本次生成的全局设计参考稿。只用于校准整体构图、标题/价格/贴片的视觉关系；准确文字以后面的 copy_items 和原生文字层为准。"})
        user_content.append({"type": "image_url", "image_url": {"url": _image_data_url(reference_path, 720)}})
    brand_references = _brand_reference_images(brand)
    if brand_references:
        user_content.append({"type": "text", "text": "以下图片是品牌排版参考，只学习构图、层级、字体气质和装饰语言；不要复制其中的文字、价格、日期、人物或 Logo。"})
        for path in brand_references[:3]:
            user_content.append({"type": "image_url", "image_url": {"url": _image_data_url(path, 512)}})
    raw = client.chat_json([
        {"role": "system", "content": "你只输出一个合法 JSON 对象，不要 Markdown、解释或代码块。"},
        {"role": "user", "content": user_content},
    ], model=settings.core_ai_design_model)
    if not isinstance(raw.get("layers"), list):
        raise CoreAIError("design_invalid_layout", "排版模型没有返回 layers")
    normalized = _normalize_layout_layers(raw.get("layers"), canvas=canvas, copy_items=copy_items, copy=copy)
    if not any(item["role"] == "headline" for item in normalized):
        raise CoreAIError("design_invalid_layout", "排版模型漏掉了主标题")
    logo_box = raw.get("logo_box") if has_logo else None
    return {
        "layers": normalized,
        "logo_box": _clamp_box(logo_box, canvas, default={"x": int(canvas["width"] * 0.84), "y": int(canvas["height"] * 0.04), "width": int(canvas["width"] * 0.12), "height": int(canvas["width"] * 0.12)}) if has_logo else None,
        "notes": str(raw.get("notes") or "")[:300],
        "source": "model",
    }


def repair_layout(
    layout: dict[str, Any],
    canvas: dict[str, int],
    copy: dict[str, str] | None = None,
    *,
    copy_items: list[dict[str, Any]] | None = None,
    has_logo: bool,
    brand: BrandStyle | None = None,
) -> dict[str, Any]:
    items = [item for item in layout.get("layers", []) if isinstance(item, dict)]
    if not items:
        return fallback_layout(canvas, copy or {}, has_logo=has_logo, brand=brand, copy_items=copy_items)
    normalized = _normalize_layout_layers(items, canvas=canvas, copy_items=copy_items or [], copy=copy)
    if not normalized:
        return fallback_layout(canvas, copy or {}, has_logo=has_logo, brand=brand, copy_items=copy_items)
    layout["layers"] = normalized
    layout.setdefault("source", "model")
    return layout

def _resolve_logo_overlap(layout: dict[str, Any], canvas: dict[str, int]) -> None:
    logo = layout.get("logo_box")
    if not isinstance(logo, dict):
        return
    gap = max(16, int(canvas["width"] * 0.018))
    logo_left = int(logo["x"])
    logo_right = logo_left + int(logo["width"])
    logo_top = int(logo["y"])
    logo_bottom = logo_top + int(logo["height"])
    for item in layout.get("layers", []):
        box = item.get("box")
        if not isinstance(box, dict):
            continue
        left, top = int(box["x"]), int(box["y"])
        right, bottom = left + int(box["width"]), top + int(box["height"])
        if not (left < logo_right and right > logo_left and top < logo_bottom and bottom > logo_top):
            continue
        new_width = logo_left - gap - left
        if new_width >= int(canvas["width"] * 0.18):
            box["width"] = new_width
            continue
        new_top = logo_bottom + gap
        if new_top + int(box["height"]) <= canvas["height"]:
            box["y"] = new_top
        else:
            box["width"] = max(120, logo_left - gap - left)


def fit_logo_box(layout: dict[str, Any], canvas: dict[str, int], *, logo_aspect: float) -> None:
    logo = layout.get("logo_box")
    if not isinstance(logo, dict):
        return
    width = max(24, min(int(canvas["width"] * 0.3), int(logo.get("width") or 150)))
    height = max(16, int(width / max(0.1, logo_aspect)))
    if int(logo.get("y", 0)) + height > canvas["height"]:
        height = max(16, canvas["height"] - int(logo.get("y", 0)))
        width = max(16, int(height * logo_aspect))
    logo["width"], logo["height"] = int(width), int(height)
    logo["x"] = int(max(0, min(canvas["width"] - width, logo.get("x", 0))))
    logo["y"] = int(max(0, min(canvas["height"] - height, logo.get("y", 0))))


def fallback_layout(
    canvas: dict[str, int],
    copy: dict[str, str],
    *,
    has_logo: bool,
    brand: BrandStyle | None = None,
    copy_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Deterministic safe layout used only when the model or initial plan fails."""
    width, height = int(canvas["width"]), int(canvas["height"])
    palette = DEFAULT_PALETTE
    fonts = DEFAULT_FONTS
    items = copy_items or [{"id": f"copy_{role}_{index + 1}", "role": role, "text": text, "source": "creative"} for index, (role, text) in enumerate(copy.items())]
    items = [item for item in items if str(item.get("text") or "").strip()]
    if not items:
        items = [{"id": "copy_headline_1", "role": "headline", "text": "人气美食", "source": "creative"}]
    margin_x = int(width * 0.07)
    top = int(height * 0.08) if has_logo else int(height * 0.10)
    usable_width = int(width * 0.78 if has_logo else width * 0.86)
    layers: list[dict[str, Any]] = []
    y = top
    for index, item in enumerate(items):
        role = str(item.get("role") or "headline")
        content = str(item.get("text") or "")
        is_headline = role == "headline"
        size = max(30, int(width * (0.13 if is_headline else 0.045)))
        box_height = max(70, int(size * (1.8 if is_headline else 1.4)))
        if y + box_height > height * 0.90:
            y = max(top, int(height * 0.62))
        layers.append({
            "id": f"layer_{item.get('id', index)}",
            "copy_id": item.get("id") or f"copy_{role}_{index + 1}",
            "role": role,
            "content": content,
            "source": item.get("source") or "creative",
            "box": {"x": margin_x, "y": y, "width": usable_width, "height": box_height},
            "font_size": size,
            "font_weight": 900 if is_headline else 700 if role in {"price", "offer", "cta"} else 500,
            "font_family": fonts.get("display", "brush") if role in {"headline", "brand"} else fonts.get("price", "sans") if role == "price" else fonts.get("body", "sans"),
            "color": palette.get("highlight", "#FFE36B") if role == "price" else palette.get("cream", "#FFF4DF"),
            "align": "left",
            "vertical_align": "top",
            "line_height": 1.05 if is_headline else 1.25,
            "letter_spacing": 0,
            "rotation": 0,
            "z_index": 100 + index * 10,
            "stroke": None,
            "shadow": None,
            "background": None,
        })
        y += box_height + int(height * 0.018)
    logo_box = None
    if has_logo:
        size = int(width * 0.12)
        logo_box = {"x": width - margin_x - size, "y": int(height * 0.035), "width": size, "height": size}
    return {"layers": layers, "logo_box": logo_box, "notes": "safe deterministic fallback", "source": "fallback-safe"}

def promo_layout(
    canvas: dict[str, int],
    copy: dict[str, str],
    *,
    has_logo: bool,
    brand: BrandStyle | None = None,
) -> dict[str, Any]:
    """Legacy promotional layout used only for A/B benchmarks and old tests."""
    width, height = int(canvas["width"]), int(canvas["height"])
    palette = DEFAULT_PALETTE
    fonts = DEFAULT_FONTS
    margin = int(width * 0.06)
    layers: list[dict[str, Any]] = []

    def add(role: str, box: dict[str, int], **style: Any) -> None:
        content = str(copy.get(role) or "").strip()
        if not content:
            return
        layers.append({
            "id": f"text_{role}",
            "copy_id": f"copy_{role}_1",
            "role": role,
            "content": content,
            "box": box,
            "font_size": style.get("font_size", 60),
            "font_weight": style.get("font_weight", 700),
            "font_family": style.get("font_family", fonts.get("body", "sans")),
            "color": style.get("color", palette.get("cream", "#FFF4DF")),
            "align": style.get("align", "left"),
            "vertical_align": "top",
            "line_height": style.get("line_height", 1.1),
            "letter_spacing": 0,
            "rotation": style.get("rotation", 0),
            "z_index": 100 + len(layers) * 10,
            "stroke": style.get("stroke"),
            "shadow": style.get("shadow"),
            "background": style.get("background"),
        })

    if "brand" in copy:
        add("brand", {"x": margin, "y": int(height * 0.04), "width": int(width * 0.5), "height": int(height * 0.06)}, font_size=max(24, int(width * 0.03)), color=palette.get("cream", "#FFF4DF"))
    if "headline" in copy:
        add("headline", {"x": margin, "y": int(height * 0.12), "width": int(width * 0.85), "height": int(height * 0.18)}, font_size=max(80, int(width * 0.16)), font_weight=900, font_family=fonts.get("display", "brush"), color=palette.get("cream", "#FFF4DF"), stroke={"width": max(3, int(width * 0.005)), "color": palette.get("accent", "#B12310")}, shadow={"blur": 12, "offset_x": 0, "offset_y": 6, "color": "#00000099"})
    if "subheadline" in copy:
        add("subheadline", {"x": margin, "y": int(height * 0.32), "width": int(width * 0.7), "height": int(height * 0.07)}, font_size=max(30, int(width * 0.04)), color=palette.get("cream", "#FFF4DF"))
    if "offer" in copy:
        add("offer", {"x": margin, "y": int(height * 0.42), "width": int(width * 0.55), "height": int(height * 0.08)}, font_size=max(30, int(width * 0.04)), font_weight=900, color=palette.get("white", "#FFFFFF"), rotation=-3, background={"color": palette.get("accent_bright", "#D92D20") + "FF", "radius": 20, "padding": 14})
    if "price" in copy:
        add("price", {"x": margin, "y": int(height * 0.66), "width": int(width * 0.65), "height": int(height * 0.14)}, font_size=max(60, int(width * 0.10)), font_weight=900, color=palette.get("highlight", "#FFE36B"), rotation=-2, background={"color": palette.get("accent", "#B12310") + "FF", "radius": 12, "padding": 16})
    if "details" in copy:
        add("details", {"x": margin, "y": int(height * 0.84), "width": int(width * 0.72), "height": int(height * 0.07)}, font_size=max(20, int(width * 0.025)), color=palette.get("cream", "#FFF4DF"))
    if "cta" in copy:
        add("cta", {"x": int(width * 0.62), "y": int(height * 0.85), "width": int(width * 0.32), "height": int(height * 0.08)}, font_size=max(22, int(width * 0.03)), font_weight=700, color=palette.get("cta_text", "#153F2A"), rotation=3, background={"color": palette.get("cream", "#FFF4DF") + "FF", "radius": 22, "padding": 12})
    if "legal" in copy:
        add("legal", {"x": margin, "y": int(height * 0.95), "width": int(width * 0.88), "height": int(height * 0.04)}, font_size=max(16, int(width * 0.018)), color=palette.get("cream", "#FFF4DF"))
    logo_box = None
    if has_logo:
        size = int(width * 0.12)
        logo_box = {"x": width - margin - size, "y": int(height * 0.045), "width": size, "height": size}
    return {"layers": layers, "logo_box": logo_box, "notes": "legacy promo baseline", "source": "promo-reflow"}


def layout_to_layers(
    layout: dict[str, Any],
    *,
    canvas: dict[str, int],
    logo_asset_id: str | None,
    brand: BrandStyle | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    text_layers: list[dict[str, Any]] = []
    role_counts: dict[str, int] = {}
    seen_copy_ids: set[str] = set()
    for index, item in enumerate(layout.get("layers", [])):
        role = str(item.get("role") or "headline")
        content = str(item.get("content") or "").strip()
        if role not in ROLE_NAMES or not content:
            continue
        item_copy_id = str(item.get("copy_id") or "")
        if item_copy_id and item_copy_id in seen_copy_ids:
            continue
        if item_copy_id:
            seen_copy_ids.add(item_copy_id)
        role_counts[role] = role_counts.get(role, 0) + 1
        layer_id = str(item.get("id") or f"layer_text_{role}_{role_counts[role]}")
        name = ROLE_NAMES[role] + (f" {role_counts[role]}" if role_counts[role] > 1 else "")
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
            layer_id=layer_id,
            name=name,
            role=role,
            content=content,
            box=item.get("box") or {},
            style=style,
            z_index=int(item.get("z_index") or 100 + index * 10),
            rotation=float(item.get("rotation") or 0),
            copy_id=str(item.get("copy_id") or "") or None,
        )
        source = str(item.get("source") or "creative")
        layer["provenance"]["copy_source"] = source
        palette = DEFAULT_PALETTE
        if not layer["style"].get("runs") and role == "headline" and layer["style"].get("font_family") == "brush" and 4 <= len(content) <= 10:
            midpoint = max(1, len(content) // 2)
            layer["style"]["runs"] = [
                {"text": content[:midpoint], "color": palette.get("primary_deep", "#0D4317")},
                {"text": content[midpoint:], "color": palette.get("accent", "#B12310")},
            ]
            layer["style"]["stroke"] = {"width": max(4, int(canvas["width"] * 0.006)), "color": palette.get("cream", "#FFF4DF")}
            layer["style"]["shadow"] = {"blur": 10, "offset_x": 0, "offset_y": 6, "color": "#00000088"}
        if not layer["style"].get("runs") and role == "price":
            match = re.match(r"^([$¥€]?\s?\d+(?:[.,]\d+)?)(.*)$", content)
            if match and match.group(2):
                layer["style"]["runs"] = [
                    {"text": match.group(1), "color": palette.get("cream", "#FFF4DF")},
                    {"text": match.group(2), "color": palette.get("highlight", "#FFE36B")},
                ]
        text_layers.append(layer)
    logo = None
    if logo_asset_id and layout.get("logo_box"):
        logo = design_utils.make_image_layer(
            layer_id="logo_brand",
            name="品牌 Logo",
            asset_id=logo_asset_id,
            role="logo",
            box=layout["logo_box"],
            z_index=900,
            fit="contain",
            render_hint="overlay",
        )
    return text_layers, logo


def brand_hint_from_asset(path: Path | None) -> str | None:
    if path is None:
        return None
    name = path.stem.lower()
    if "seejoy" in name or "喜聚" in path.stem:
        return "SEEJOY 喜聚"
    return None
