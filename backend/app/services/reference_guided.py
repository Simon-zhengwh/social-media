from __future__ import annotations

import copy
import hashlib
import io
import json
import logging
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from app.config import Settings
from app.services import brand_style
from app.services import design as design_utils
from app.services import storage
from app.services.canvas import gateway_image_size
from app.services.core_ai import CoreAIClient
from app.services.image_ref import ImageRef, image_ref_hash, image_ref_name
from app.services.object_store import ObjectStore

logger = logging.getLogger(__name__)


class ComponentGenerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class GeneratedAsset:
    asset_id: str
    width: int
    height: int
    coverage: float
    cache_key: str | None = None
    source: str = "model"

    def summary(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "width": self.width,
            "height": self.height,
            "coverage": round(self.coverage, 4),
            "cache_key": self.cache_key,
            "source": self.source,
        }


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cache_lookup(db: Any, key: str) -> dict[str, Any] | None:
    entry = db.component_cache.find_one({"_id": key})
    if not isinstance(entry, dict):
        return None
    asset = db.assets.find_one({"_id": entry.get("asset_id")})
    if asset is None:
        return None
    return {**entry, "asset": asset}


def _save_generated_asset(
    db: Any,
    settings: Settings,
    store: ObjectStore,
    *,
    task_id: str,
    kind: str,
    name: str,
    data: bytes,
    metadata: dict[str, Any] | None = None,
) -> GeneratedAsset:
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = source.copy()
            image.load()
    except Exception as exc:  # noqa: BLE001 - provider payload is untrusted
        raise ComponentGenerationError("生图组件无法解码") from exc
    width, height = image.size
    relative = storage.object_key(settings, "backgrounds", f"{task_id}_{name}.png")
    store.put_bytes(relative, data, "image/png")
    asset_id = f"asset_{uuid.uuid4().hex}"
    digest = hashlib.sha256(data).hexdigest()
    db.assets.insert_one(
        {
            "_id": asset_id,
            "kind": kind,
            "original_name": f"{name}.png",
            "path": relative,
            "prepared_path": relative,
            "mime_type": "image/png",
            "width": width,
            "height": height,
            "bytes": len(data),
            "sha256": digest,
            "created_at": datetime.now(timezone.utc),
            "metadata": metadata or {},
        }
    )
    return GeneratedAsset(
        asset_id=asset_id,
        width=width,
        height=height,
        coverage=float((metadata or {}).get("coverage") or 0.0),
        cache_key=(metadata or {}).get("cache_key"),
        source=str((metadata or {}).get("source") or "model"),
    )


def _mask(channel: Image.Image, predicate) -> Image.Image:
    return channel.point(lambda value: 255 if predicate(value) else 0)


def extract_transparent_artwork(data: bytes) -> tuple[Image.Image, float]:
    """Turn a magenta-screen component render into a tight transparent PNG.

    The image generator cannot be assumed to return alpha. A saturated magenta
    field is requested instead, then keyed out in Pillow. The key is deliberately
    narrow so brand red stays opaque while the screen color disappears.
    """

    try:
        with Image.open(io.BytesIO(data)) as source:
            rgb = ImageOps.exif_transpose(source).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        raise ComponentGenerationError("组件图片无法解码") from exc

    hsv = rgb.convert("HSV")
    hue, saturation, value = hsv.split()
    red, green, blue = rgb.split()

    hue_mask = _mask(hue, lambda item: 178 <= item <= 248)
    saturation_mask = _mask(saturation, lambda item: item >= 92)
    value_mask = _mask(value, lambda item: item >= 34)
    low_green_mask = _mask(green, lambda item: item <= 118)
    rb_mask = ImageChops.darker(red, blue).point(lambda item: 255 if item >= 72 else 0)

    screen_mask = ImageChops.multiply(hue_mask, saturation_mask)
    screen_mask = ImageChops.multiply(screen_mask, value_mask)
    screen_mask = ImageChops.multiply(screen_mask, low_green_mask)
    screen_mask = ImageChops.multiply(screen_mask, rb_mask)
    # Expand by one pixel to remove the compressed magenta fringe, then feather.
    screen_mask = screen_mask.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(1.15))
    alpha = ImageOps.invert(screen_mask)

    coverage = sum(alpha.histogram()[24:]) / float(rgb.width * rgb.height)
    if coverage < 0.006:
        raise ComponentGenerationError("组件主体过小，疑似生图未遵循品红底要求")
    if coverage > 0.92:
        raise ComponentGenerationError("组件主体过大，品红底去除失败")

    rgba = rgb.convert("RGBA")
    # Despill only purplish edge pixels, never saturated brand red.
    tint_mask = ImageChops.multiply(
        _mask(red, lambda item: item >= 58), ImageChops.darker(red, blue)
    )
    tint_mask = ImageChops.multiply(tint_mask, _mask(green, lambda item: item <= 112))
    tint_mask = tint_mask.filter(ImageFilter.GaussianBlur(0.8))
    cooled_green = green.point(lambda item: min(255, item + 18))
    green = Image.composite(cooled_green, green, tint_mask)
    rgba = Image.merge("RGBA", (red, green, blue, alpha))

    visible = alpha.point(lambda item: 255 if item > 8 else 0)
    bbox = visible.getbbox()
    if bbox is None:
        raise ComponentGenerationError("组件透明化后没有可见内容")
    pad = 8
    box = (
        max(0, bbox[0] - pad),
        max(0, bbox[1] - pad),
        min(rgba.width, bbox[2] + pad),
        min(rgba.height, bbox[3] + pad),
    )
    cropped = rgba.crop(box)
    if max(cropped.size) > 1400:
        cropped.thumbnail((1400, 1400), Image.Resampling.LANCZOS)
    coverage = sum(cropped.getchannel("A").histogram()[24:]) / float(cropped.width * cropped.height)
    return cropped, coverage


def baked_light_glyph_ratio(image: Image.Image) -> float:
    """Estimate large cream/white glyph regions inside a transparent component.

    Small paint flecks survive the opening; thick baked typography does not.
    Price artwork must stay purely material, because native price text is added
    later and must never overlap a second baked price.
    """

    rgba = image.convert("RGBA")
    red, green, blue, alpha = rgba.split()
    light = ImageChops.multiply(
        ImageChops.multiply(
            red.point(lambda value: 255 if value > 160 else 0),
            green.point(lambda value: 255 if value > 140 else 0),
        ),
        blue.point(lambda value: 255 if value > 105 else 0),
    )
    visible = alpha.point(lambda value: 255 if value > 24 else 0)
    light = ImageChops.multiply(light, visible)
    core = light.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(9))
    ratio = sum(core.histogram()[128:]) / float(max(1, rgba.width * rgba.height))
    return ratio


def _copy_lines(copy_items: list[dict[str, Any]]) -> str:
    lines = []
    for item in copy_items:
        text = str(item.get("text") or "").strip()
        if text:
            lines.append(f"- {item.get('role') or 'copy'}: {text}")
    return "\n".join(lines)


def reference_prompt(
    *,
    brand: brand_style.BrandStyle,
    brief: str,
    copy_items: list[dict[str, Any]],
    intent: dict[str, Any],
    ratio: str,
) -> str:
    return (
        "Create a finished commercial social-media poster design reference. "
        "This image is the visual north star for later layered generation; it is not the final deliverable.\n"
        f"Brand: {brand.name}.\n"
        f"Canvas ratio: {ratio}.\n"
        f"Brief: {brief or 'Create an appetite-driven restaurant promotion poster.'}\n"
        f"Design intent: {json.dumps(intent, ensure_ascii=False)}\n"
        "Art direction:\n"
        f"{brand.layout_guide[:1800]}\n"
        "Use the supplied product photos as the main food/focal references and the supplied brand poster as the style reference. "
        "Do not copy its words, price, date, people, logo, or exact layout. Build one coherent composition: oversized Chinese brush title, "
        "real food photography integrated into the same scene, irregular hand-painted price ribbon, a small offer sticker, "
        "natural handwritten annotations, cream paper accents, deep forest green and brand red. Avoid UI cards, dashboard panels, "
        "plain rounded rectangles, neat grids, and pasted-looking blocks.\n"
        "Use these exact texts as content anchors; do not invent extra numbers, prices, dates, addresses, or claims:\n"
        f"{_copy_lines(copy_items)}\n"
        "Chinese typography may be approximate in this reference; accuracy will be restored later with native text layers.\n"
        "No mockup frame, no watermark, no extra logo, no QR code."
    )


def generate_global_reference(
    db: Any,
    settings: Settings,
    store: ObjectStore,
    client: CoreAIClient,
    *,
    task_id: str,
    brand: brand_style.BrandStyle,
    brief: str,
    copy_items: list[dict[str, Any]],
    intent: dict[str, Any],
    ratio: str,
    canvas: dict[str, int],
    product_paths: list[Path | ImageRef],
) -> GeneratedAsset:
    brand_references = list(brand.reference_inputs) or list(brand.reference_paths)
    style_paths = [
        path for path in brand_references
        if "reference-2" in image_ref_name(path).lower()
    ]
    if not style_paths:
        style_paths = brand_references[:1]
    references = list(product_paths[:4]) + style_paths[:1]
    prompt = reference_prompt(
        brand=brand,
        brief=brief,
        copy_items=copy_items,
        intent=intent,
        ratio=ratio,
    )
    source_hashes = [image_ref_hash(path) for path in references]
    key = _sha256_text(json.dumps({
        "kind": "global_reference",
        "prompt": prompt,
        "model": settings.core_ai_image_model,
        "quality": settings.core_ai_image_quality,
        "sources": source_hashes,
        "brand": brand.id,
    }, ensure_ascii=False, sort_keys=True))
    if settings.component_generation_cache_enabled:
        cached = _cache_lookup(db, key)
        if cached is not None:
            asset = cached["asset"]
            return GeneratedAsset(
                asset_id=str(asset["_id"]),
                width=int(asset.get("width") or 0),
                height=int(asset.get("height") or 0),
                coverage=1.0,
                cache_key=key,
                source="cache",
            )

    size = gateway_image_size(canvas["width"], canvas["height"])
    if references:
        data = client.edit_image(prompt, references, size=size)
    else:
        data = client.generate_image(prompt, size=size)
    art = _save_generated_asset(
        db,
        settings,
        store,
        task_id=task_id,
        kind="design_reference",
        name="design_reference",
        data=data,
        metadata={"cache_key": key, "source": "model", "brief": brief[:500]},
    )
    if settings.component_generation_cache_enabled:
        db.component_cache.insert_one({
            "_id": key,
            "asset_id": art.asset_id,
            "kind": "global_reference",
            "created_at": datetime.now(timezone.utc),
        })
    return art


def clean_plate_prompt(*, brand: brand_style.BrandStyle, brief: str) -> str:
    return (
        "Using the supplied finished poster reference, rebuild it as a clean photographic plate for precise native typography.\n"
        "Keep the exact composition, camera angle, food placement, plate cropping, lighting, depth of field, paper texture and color mood. "
        "Remove every letter, Chinese character, number, price, badge lettering, logo, QR code, watermark, mockup frame and text placeholder. "
        "Also remove painted brush-lettering and letter-shaped fragments, but keep non-letter brush textures, torn paper, ribbons and photographic scene elements "
        "only when they work as background texture rather than a separate UI card. Never add new readable symbols.\n"
        f"Brand photography guide:\n{brand.background_guide[:1800]}\n"
        f"Original brief for color/mood only: {brief[:800]}\n"
        "The result must be one continuous commercial food photograph, not a collage and not a template with empty rectangles."
    )


def generate_clean_plate(
    settings: Settings,
    client: CoreAIClient,
    *,
    reference_path: Path | ImageRef,
    brand: brand_style.BrandStyle,
    brief: str,
    canvas: dict[str, int],
) -> bytes:
    return client.edit_image(
        clean_plate_prompt(brand=brand, brief=brief),
        [reference_path],
        size=gateway_image_size(canvas["width"], canvas["height"]),
    )


def harmonize_scene_image(data: bytes, brand: brand_style.BrandStyle) -> bytes:
    """Apply a restrained deterministic grade so the clean plate feels like one scene.

    This is image processing, not design generation: it only unifies edge density,
    contrast and the brand's deep-green atmosphere. It must not change food content.
    """

    try:
        with Image.open(io.BytesIO(data)) as source:
            image = source.convert("RGB")
    except Exception:  # noqa: BLE001 - a failed grade must not break generation
        return data
    image = ImageEnhance.Color(image).enhance(1.025)
    image = ImageEnhance.Contrast(image).enhance(1.018)
    vignette = Image.radial_gradient("L").resize(image.size, Image.Resampling.BICUBIC)
    vignette = vignette.filter(ImageFilter.GaussianBlur(max(8, image.width // 80)))
    edge_alpha = vignette.point(lambda value: int(max(0.0, value - 118) * 0.22))
    edge_color = brand_style.DEFAULT_PALETTE.get("primary_deep", "#08300F")
    try:
        rgb = tuple(int(edge_color[index:index + 2], 16) for index in (1, 3, 5))
    except (TypeError, ValueError):
        rgb = (8, 48, 15)
    tint = Image.new("RGB", image.size, rgb)
    tint.putalpha(edge_alpha)
    image = Image.alpha_composite(image.convert("RGBA"), tint).convert("RGB")
    return storage.encode_png(image)


_COMPONENT_DIRECTIONS = {
    "title": (
        "an isolated oversized hand-painted Chinese poster title backing: two overlapping dry-brush strokes in deep forest green and brand red, "
        "with cream and warm-yellow paint speckles, torn bristle edges and visible paper texture. Wide horizontal composition, no readable marks."
    ),
    "price": (
        "an isolated irregular promotional price ribbon: one confident brand-red dry-brush ribbon with torn painted edges, a darker red inner fold, "
        "and a few warm-yellow accent strokes. Wide horizontal composition, no frame, no card, no readable marks."
    ),
    "offer": (
        "an isolated small offer sticker: an irregular hand-painted red or deep-green cloud/flower badge with rough cream paint outline and tiny yellow highlights. "
        "Compact shape, no container card, no frame, no readable marks."
    ),
}


def describe_target(box: dict[str, int], canvas: dict[str, int]) -> dict[str, Any]:
    center_x = (float(box.get("x", 0)) + float(box.get("width", 0)) / 2) / max(1, canvas["width"])
    center_y = (float(box.get("y", 0)) + float(box.get("height", 0)) / 2) / max(1, canvas["height"])
    horizontal = "left" if center_x < 0.36 else "right" if center_x > 0.64 else "center"
    vertical = "top" if center_y < 0.34 else "bottom" if center_y > 0.66 else "middle"
    return {
        "horizontal_region": horizontal,
        "vertical_region": vertical,
        "center_x_ratio": round(center_x, 4),
        "center_y_ratio": round(center_y, 4),
        "width_ratio": round(float(box.get("width", 0)) / max(1, canvas["width"]), 4),
        "height_ratio": round(float(box.get("height", 0)) / max(1, canvas["height"]), 4),
    }


def make_context_images(
    current_image: Image.Image,
    *,
    target_box: dict[str, int],
    canvas: dict[str, int],
) -> list[Path]:
    """Create guide + local crop references without any paid model call."""

    paths: list[Path] = []
    source = current_image.convert("RGB")
    guide = source.copy()
    _draw_target_highlight(guide, target_box, canvas)
    guide.thumbnail((720, 720), Image.Resampling.LANCZOS)
    guide_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
    guide_file.close()
    guide.save(guide_file.name, format="JPEG", quality=84, optimize=True)
    paths.append(Path(guide_file.name))

    left = int(target_box.get("x", 0))
    top = int(target_box.get("y", 0))
    width = max(32, int(target_box.get("width", 0)))
    height = max(32, int(target_box.get("height", 0)))
    pad_x = int(width * 0.28)
    pad_y = int(height * 0.45)
    crop_box = (
        max(0, left - pad_x),
        max(0, top - pad_y),
        min(source.width, left + width + pad_x),
        min(source.height, top + height + pad_y),
    )
    crop = source.crop(crop_box)
    crop.thumbnail((640, 640), Image.Resampling.LANCZOS)
    crop_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
    crop_file.close()
    crop.save(crop_file.name, format="JPEG", quality=84, optimize=True)
    paths.append(Path(crop_file.name))
    return paths


def _draw_target_highlight(
    image: Image.Image,
    target_box: dict[str, int],
    canvas: dict[str, int],
) -> None:
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    x = int(target_box.get("x", 0))
    y = int(target_box.get("y", 0))
    width = max(16, int(target_box.get("width", 0)))
    height = max(16, int(target_box.get("height", 0)))
    draw.rounded_rectangle(
        (x, y, x + width, y + height),
        radius=max(8, int(min(width, height) * 0.08)),
        fill=(255, 227, 107, 54),
        outline=(255, 244, 223, 235),
        width=max(4, int(canvas["width"] * 0.005)),
    )
    composed = Image.alpha_composite(image.convert("RGBA"), overlay).convert(image.mode)
    image.paste(composed)


def component_prompt(
    *,
    kind: str,
    brand: brand_style.BrandStyle,
    intent: dict[str, Any],
    text_hint: str,
    variation: int = 0,
    target: dict[str, Any] | None = None,
    context_count: int = 0,
) -> str:
    direction = _COMPONENT_DIRECTIONS.get(kind, _COMPONENT_DIRECTIONS["offer"])
    variation_line = (
        f"Provide a fresh variation number {variation} while preserving the same visual language. "
        if variation else ""
    )
    target_line = json.dumps(target or {}, ensure_ascii=False)
    context_line = (
        "Reference image 1 is the style/material reference (for price and offer it may be a clean text-free brush component). "
        "Reference image 2 (when present) is the CURRENT POSTER COMPOSITE with the target area subtly highlighted; "
        "the yellow highlight is only a placement guide and must never appear in the output. "
        "Reference image 3 (when present) is a local crop of the surrounding scene and neighboring elements. "
        "Match their light, material, paper grain, edge rhythm, color temperature and brush language without copying any text. "
        if context_count else
        "Reference image 1 is the global visual north star. Match its light, material, color temperature and brush language. "
    )
    return (
        "Create ONLY one isolated poster visual component on a perfectly flat, uniform pure magenta background (#FF00FF).\n"
        f"Component role: {kind}. {direction}\n"
        + (
            "Native price typography will be added later by the renderer. Do NOT generate any currency symbol, digit, unit, Chinese character or readable mark. "
            "Even when supplied references contain a price, treat it as forbidden content.\n"
            if kind == "price" else
            f"It will sit behind native text added later: {text_hint or 'poster copy'}. Never render that text yourself.\n"
        )
        + f"Target region in the current poster: {target_line}. Use this to match the scale, direction and light direction of the surrounding design.\n"
        f"{context_line}"
        f"Design intent: {json.dumps(intent, ensure_ascii=False)}. {variation_line}"
        "The component should feel painted onto the same physical poster as the scene: coherent edge wear, material grain, restrained highlights and natural depth. "
        "Do not create a clean plastic sticker, a rectangular card, a UI pill, or a detached graphic with no relation to the scene. "
        "Absolutely no letters, Chinese characters, numbers, logos, watermarks, background shadows, border, frame, mockup or white plate. "
        "Do not copy any typography visible in the references; the component must remain purely non-readable material. "
        "Keep every non-artwork pixel exactly pure magenta so it can be keyed out cleanly. "
        "The artwork must be one coherent painted object, not several disconnected stickers."
    )


def generate_component_art(
    db: Any,
    settings: Settings,
    store: ObjectStore,
    client: CoreAIClient,
    *,
    task_id: str,
    kind: str,
    brand: brand_style.BrandStyle,
    intent: dict[str, Any],
    reference_path: Path | ImageRef,
    text_hint: str,
    context_paths: list[Path] | None = None,
    target: dict[str, Any] | None = None,
    variation: int = 0,
) -> GeneratedAsset | None:
    if not settings.component_generation_enabled:
        return None
    contexts = list(context_paths or [])
    prompt = component_prompt(
        kind=kind,
        brand=brand,
        intent=intent,
        text_hint=text_hint,
        variation=variation,
        target=target,
        context_count=len(contexts),
    )
    context_hashes: list[str] = [image_ref_hash(reference_path)]
    context_hashes.extend(image_ref_hash(path) for path in contexts)
    key = _sha256_text(json.dumps({
        "kind": kind,
        "prompt": prompt,
        "reference": context_hashes[0] if context_hashes else "",
        "contexts": context_hashes[1:],
        "target": target or {},
        "model": settings.core_ai_image_model,
        "quality": settings.core_ai_image_quality,
        "variation": variation,
        "brand": brand.id,
    }, ensure_ascii=False, sort_keys=True))
    if settings.component_generation_cache_enabled and variation == 0:
        cached = _cache_lookup(db, key)
        if cached is not None:
            asset = cached["asset"]
            return GeneratedAsset(
                asset_id=str(asset["_id"]),
                width=int(asset.get("width") or 0),
                height=int(asset.get("height") or 0),
                coverage=float((asset.get("metadata") or {}).get("coverage") or 0.0),
                cache_key=key,
                source="cache",
            )

    references = [reference_path, *contexts]
    last_error: Exception | None = None
    for attempt in range(2):
        request_prompt = prompt
        if attempt:
            request_prompt += (
                "\nRETRY: the previous attempt failed alpha extraction. Use a larger, centered painted object, "
                "a perfectly flat #FF00FF field, no gradients or shadows, and leave 10% clean magenta margin."
            )
        try:
            data = client.edit_image(request_prompt, references, size="1024x1024")
            artwork, coverage = extract_transparent_artwork(data)
            if kind == "price":
                glyph_ratio = baked_light_glyph_ratio(artwork)
                if glyph_ratio > 0.05:
                    raise ComponentGenerationError(
                        f"价格组件疑似烘培了可读文字（高亮字形区域 {glyph_ratio:.1%}）"
                    )
            encoded = storage.encode_png(artwork)
            art = _save_generated_asset(
                db,
                settings,
                store,
                task_id=task_id,
                kind="component",
                name=f"component_{kind}_{variation or 0}",
                data=encoded,
                metadata={
                    "component_type": kind,
                    "cache_key": key,
                    "coverage": coverage,
                    "source": "model",
                    "reference_asset_id": None,
                    "prompt": request_prompt[:4000],
                    "context_image_count": len(contexts),
                    "target": target or {},
                },
            )
            if settings.component_generation_cache_enabled and variation == 0:
                db.component_cache.insert_one({
                    "_id": key,
                    "asset_id": art.asset_id,
                    "kind": f"component:{kind}",
                    "created_at": datetime.now(timezone.utc),
                })
            return GeneratedAsset(
                asset_id=art.asset_id,
                width=art.width,
                height=art.height,
                coverage=coverage,
                cache_key=key,
                source="model",
            )
        except Exception as exc:  # noqa: BLE001 - fallback is intentional
            last_error = exc
            logger.warning("component generation failed (%s, attempt %s): %s", kind, attempt + 1, exc)
    logger.warning("component generation exhausted for %s: %s", kind, last_error)
    return None


def _union_box(layers: list[dict[str, Any]], canvas: dict[str, int], *, pad_x: float, pad_y: float) -> dict[str, int]:
    left = min(float(layer["position"]["x"]) for layer in layers)
    top = min(float(layer["position"]["y"]) for layer in layers)
    right = max(float(layer["position"]["x"]) + float(layer["size"]["width"]) for layer in layers)
    bottom = max(float(layer["position"]["y"]) + float(layer["size"]["height"]) for layer in layers)
    pad_w = int(canvas["width"] * pad_x)
    pad_h = int(canvas["height"] * pad_y)
    x = max(0, int(left - pad_w))
    y = max(0, int(top - pad_h))
    width = min(canvas["width"] - x, max(24, int(right - left + pad_w * 2)))
    height = min(canvas["height"] - y, max(24, int(bottom - top + pad_h * 2)))
    return {"x": x, "y": y, "width": width, "height": height}


_COMPONENT_GROUPS = (
    ("title", {"headline"}, "headline", "component_headline", "主标题视觉组件", 0.025, 0.012),
    ("price", {"price"}, "promotion_price", "component_price", "价格视觉组件", 0.035, 0.016),
    ("offer", {"offer"}, "offer_badge", "component_offer", "优惠贴片组件", 0.025, 0.012),
)


def plan_component_groups(
    text_layers: list[dict[str, Any]],
    canvas: dict[str, int],
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    claimed: set[str] = set()
    for kind, roles, semantic, component_id, name, pad_x, pad_y in _COMPONENT_GROUPS:
        matches = [
            layer for layer in text_layers
            if layer.get("role") in roles and str(layer.get("id")) not in claimed
        ]
        if not matches:
            continue
        box = _union_box(matches, canvas, pad_x=pad_x, pad_y=pad_y)
        groups.append({
            "kind": kind,
            "roles": roles,
            "semantic_type": semantic,
            "component_id": component_id,
            "name": name,
            "layers": matches,
            "box": box,
            "target": describe_target(box, canvas),
        })
        claimed.update(str(layer.get("id")) for layer in matches)
    return groups


def component_from_group(group: dict[str, Any], art: GeneratedAsset) -> dict[str, Any]:
    layers = list(group["layers"])
    box = dict(group["box"])
    kind = str(group["kind"])
    name = str(group["name"])
    component_id = str(group["component_id"])
    primary = max(layers, key=lambda item: float(item["size"]["width"]) * float(item["size"]["height"]))
    rotation = float(primary.get("rotation") or 0)
    children: list[dict[str, Any]] = []
    art_child = {
        "id": f"{component_id}_art",
        "type": "image",
        "name": f"{name}笔刷素材",
        "role": "decoration",
        "asset_id": art.asset_id,
        "position": {"x": 0, "y": 0},
        "size": {"width": box["width"], "height": box["height"]},
        "opacity": 1.0,
        "rotation": 0.0,
        "fit": "cover" if kind == "price" else "contain",
        "z_index": 1,
        "visible": True,
        "locked": True,
        "render_hint": "overlay",
        "edit_policy": {"ai_locked": True, "manual_edited": False, "created_by": "agent"},
        "provenance": {"created_by": "agent", "component_art": True, **art.summary()},
    }
    children.append(art_child)
    for index, layer in enumerate(sorted(layers, key=lambda item: float(item.get("z_index", 0)))):
        child = copy.deepcopy(layer)
        child["id"] = f"{component_id}_{layer.get('id')}"
        child["position"] = {
            "x": int(float(layer["position"]["x"]) - box["x"]),
            "y": int(float(layer["position"]["y"]) - box["y"]),
        }
        child["rotation"] = float(layer.get("rotation") or 0) - rotation
        child["z_index"] = 20 + index * 10
        child["locked"] = False
        child["edit_policy"] = {"ai_locked": False, "manual_edited": False, "created_by": "agent"}
        children.append(child)
    texts = [str(layer.get("content") or "") for layer in layers if layer.get("content")]
    content: dict[str, Any] = {
        "text": " ".join(texts).strip(),
        "component_type": kind,
        "text_layer_ids": [layer.get("id") for layer in layers],
    }
    if kind == "price":
        content["amount"] = texts[0] if texts else ""
    component = design_utils.make_visual_component(
        component_id=component_id,
        name=name,
        semantic_type=str(group["semantic_type"]),
        content=content,
        children=children,
        box=box,
        render_mode="native",
        z_index=max(int(layer.get("z_index", 0)) for layer in layers) + 20,
        rotation=rotation,
        style={"template": f"reference-guided:{kind}"},
    )
    component["compositing"] = {
        "blend_mode": "normal",
        "opacity": 0.995,
        "feather_px": 1.4,
        "shadow": {"blur": 18, "offset_x": 0, "offset_y": 9, "color": "#00000038"},
        "color_match": 0.08,
        "anchor": "scene",
    }
    component["relations"] = {
        "scene_layer_id": "background",
        "target": group.get("target"),
        "dependent_components": [],
        "overlap_policy": "integrated",
    }
    component["provenance"].update({
        "generated_art_asset_id": art.asset_id,
        "generation_mode": "reference_guided",
        "context_aware": True,
        "regeneration": {"kind": kind, "variation": 0},
    })
    return component


def build_component_layers(
    *,
    text_layers: list[dict[str, Any]],
    canvas: dict[str, int],
    artwork: dict[str, GeneratedAsset | None],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Group native text under AI-generated visual components.

    This all-at-once helper remains for offline tests and fallback callers. The
    live pipeline uses plan_component_groups + component_from_group to provide a
    current-composite context for each dependency-ordered component.
    """

    groups = plan_component_groups(text_layers, canvas)
    grouped_ids: set[str] = set()
    components: list[dict[str, Any]] = []
    for group in groups:
        art = artwork.get(str(group["kind"]))
        if art is None:
            continue
        components.append(component_from_group(group, art))
        grouped_ids.update(str(layer.get("id")) for layer in group["layers"])
    remaining = [layer for layer in text_layers if str(layer.get("id")) not in grouped_ids]
    return remaining, components


def split_component_assets(assets: dict[str, GeneratedAsset | None]) -> dict[str, Any]:
    return {key: (value.summary() if value else {"status": "fallback"}) for key, value in assets.items()}
