from __future__ import annotations

import re
from typing import Any

from PIL import Image, ImageStat


def _rgb(value: Any, fallback: tuple[int, int, int] = (255, 255, 255)) -> tuple[int, int, int]:
    if isinstance(value, str):
        match = re.fullmatch(r"#([0-9A-Fa-f]{6})(?:[0-9A-Fa-f]{2})?", value.strip())
        if match:
            text = match.group(1)
            return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    return fallback


def _relative_luminance(color: tuple[int, int, int]) -> float:
    channels = []
    for value in color:
        channel = value / 255.0
        channels.append(
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
        )
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(first: tuple[int, int, int], second: tuple[int, int, int]) -> float:
    a = _relative_luminance(first)
    b = _relative_luminance(second)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


def assess_legibility(
    background: Image.Image,
    layers: list[dict[str, Any]],
    render_metrics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    warnings: list[dict[str, Any]] = []
    width, height = background.size
    metrics_by_id = {
        item.get("id"): item
        for item in (render_metrics or [])
        if isinstance(item, dict) and item.get("type") == "text"
    }
    for layer in layers:
        if layer.get("type") != "text" or not layer.get("visible", True):
            continue
        content = str(layer.get("content") or "").strip()
        if not content:
            continue
        position = layer.get("position") or {}
        size = layer.get("size") or {}
        x = max(0, int(position.get("x", 0)))
        y = max(0, int(position.get("y", 0)))
        right = min(width, x + max(1, int(size.get("width", 1))))
        bottom = min(height, y + max(1, int(size.get("height", 1))))
        if right <= x or bottom <= y:
            continue
        style = layer.get("style") or {}
        plate = style.get("background")
        if isinstance(plate, dict):
            backdrop = _rgb(plate.get("color"), (0, 0, 0))
        else:
            crop = background.crop((x, y, right, bottom)).convert("RGB")
            mean = ImageStat.Stat(crop).mean
            backdrop = (int(mean[0]), int(mean[1]), int(mean[2]))
        stroke = style.get("stroke") or {}
        fill_color = _rgb(style.get("color"), (255, 255, 255))
        fill_ratio = _contrast(fill_color, backdrop)
        stroke_ratio = 0.0
        if int(stroke.get("width") or 0) > 0:
            stroke_ratio = _contrast(
                _rgb(stroke.get("color"), (0, 0, 0)), backdrop
            )
        # Text is readable if either the fill or the outline separates from the
        # backdrop. Using only the stroke produced false warnings for cream
        # headlines with a dark red outline.
        ratio = max(fill_ratio, stroke_ratio)
        if ratio < 2.5:
            warnings.append(
                {
                    "code": "low_contrast",
                    "layer_id": layer.get("id"),
                    "layer_name": layer.get("name"),
                    "message": f"「{layer.get('name')}」与当前底图对比度偏低（{ratio:.1f}:1）",
                    "contrast": round(ratio, 2),
                }
            )
        metrics = metrics_by_id.get(layer.get("id"))
        if metrics and metrics.get("overflow"):
            warnings.append(
                {
                    "code": "text_overflow",
                    "layer_id": layer.get("id"),
                    "layer_name": layer.get("name"),
                    "message": f"「{layer.get('name')}」在文本框内放不下，已自动缩小但仍可能溢出",
                }
            )
    return {"warnings": warnings, "checked": True}


def _safe_bounds(
    canvas: dict[str, Any],
    placement: dict[str, Any] | None,
) -> tuple[float, float, float, float]:
    width = float(canvas.get("width") or 1)
    height = float(canvas.get("height") or 1)
    safe = (placement or {}).get("safe_area") or {}
    return (
        width * float(safe.get("left") or 0),
        height * float(safe.get("top") or 0),
        width * (1 - float(safe.get("right") or 0)),
        height * (1 - float(safe.get("bottom") or 0)),
    )


def clamp_layers_to_safe_area(
    layers: list[dict[str, Any]],
    canvas: dict[str, Any],
    placement: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(placement, dict):
        return layers
    left, top, right, bottom = _safe_bounds(canvas, placement)
    for layer in layers:
        if not isinstance(layer, dict) or not layer.get("visible", True):
            continue
        if layer.get("type") not in {"text", "visual_component"} and not (
            layer.get("type") == "image" and layer.get("role") == "logo"
        ):
            continue
        position = layer.get("position")
        size = layer.get("size")
        if not isinstance(position, dict) or not isinstance(size, dict):
            continue
        width = min(float(size.get("width") or 0), right - left)
        height = min(float(size.get("height") or 0), bottom - top)
        if width <= 0 or height <= 0:
            continue
        size["width"] = int(width)
        size["height"] = int(height)
        x = min(max(float(position.get("x") or 0), left), right - width)
        y = min(max(float(position.get("y") or 0), top), bottom - height)
        position["x"] = int(round(x))
        position["y"] = int(round(y))
    return layers


def clamp_design_to_safe_area(
    design: dict[str, Any],
    placement: dict[str, Any] | None,
) -> dict[str, Any]:
    layers = design.get("layers") or []
    if isinstance(layers, list):
        clamp_layers_to_safe_area(layers, design.get("canvas") or {}, placement)
    return design


def _platform_hard_failures(
    design: dict[str, Any],
    platform: dict[str, Any] | None,
    placement: dict[str, Any] | None,
    merchant_info: dict[str, Any] | None,
) -> list[dict[str, str]]:
    if not isinstance(platform, dict):
        return []
    failures: list[dict[str, str]] = []
    canvas = design.get("canvas") or {}
    width = int(canvas.get("width") or 1)
    height = int(canvas.get("height") or 1)
    chosen = placement if isinstance(placement, dict) else None
    if chosen is None:
        placements = platform.get("placements") or []
        default_id = str(platform.get("default_placement_id") or "")
        chosen = next(
            (item for item in placements if isinstance(item, dict) and item.get("id") == default_id),
            placements[0] if placements and isinstance(placements[0], dict) else None,
        )
    safe = (chosen or {}).get("safe_area") or {}
    left = width * float(safe.get("left") or 0)
    top = height * float(safe.get("top") or 0)
    right = width * (1 - float(safe.get("right") or 0))
    bottom = height * (1 - float(safe.get("bottom") or 0))
    for layer in design.get("layers") or []:
        if not isinstance(layer, dict) or not layer.get("visible", True):
            continue
        if layer.get("type") not in {"text", "image"}:
            continue
        if layer.get("type") == "image" and layer.get("role") != "logo":
            continue
        box = layer.get("position") or {}
        size = layer.get("size") or {}
        x = float(box.get("x") or 0)
        y = float(box.get("y") or 0)
        box_right = x + float(size.get("width") or 0)
        box_bottom = y + float(size.get("height") or 0)
        if x < left - 1 or y < top - 1 or box_right > right + 1 or box_bottom > bottom + 1:
            failures.append({
                "code": "safe_area",
                "layer_id": str(layer.get("id") or ""),
                "message": f"图层「{layer.get('name') or layer.get('id')}」超出{platform.get('name') or '平台'}安全区",
            })
    return failures


def assess_scene(
    design: dict[str, Any],
    image: Image.Image,
    metrics: list[dict[str, Any]] | None = None,
    copy_items: list[dict[str, Any]] | None = None,
    platform: dict[str, Any] | None = None,
    placement: dict[str, Any] | None = None,
    merchant_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    layers = design.get("layers") or []
    legibility = assess_legibility(image, layers, metrics)
    warnings = list(legibility.get("warnings") or [])
    hard: list[dict[str, Any]] = []
    metrics_by_id = {item.get("id"): item for item in (metrics or []) if isinstance(item, dict)}
    copy_items = copy_items or []
    text_by_copy = {
        str(layer.get("copy_id")): layer
        for layer in layers
        if layer.get("type") == "text" and layer.get("copy_id")
    }
    for item in copy_items:
        copy_id = str(item.get("id") or "")
        text = str(item.get("text") or "")
        layer = text_by_copy.get(copy_id)
        if layer is None:
            hard.append({"code": "copy_missing", "message": f"缺少文案 {copy_id}"})
            continue
        if str(layer.get("content") or "") != text:
            hard.append({"code": "copy_changed", "message": f"文案 {copy_id} 与事实源不一致"})
    canvas_width = int((design.get("canvas") or {}).get("width") or 1)
    canvas_height = int((design.get("canvas") or {}).get("height") or 1)
    for layer in layers:
        layer_id = layer.get("id")
        metric = metrics_by_id.get(layer_id) or {}
        if layer.get("type") == "image" and metric.get("skipped") == "asset_missing":
            hard.append({"code": "asset_missing", "message": f"图层 {layer_id} 图片素材缺失"})
        if layer.get("type") == "text" and layer.get("visible", True):
            bounds = metric.get("rotated_bounds") or metric.get("bounds") or {}
            if bounds:
                right = int(bounds.get("x", 0)) + int(bounds.get("width", 0))
                bottom = int(bounds.get("y", 0)) + int(bounds.get("height", 0))
                if right <= 0 or bottom <= 0 or int(bounds.get("x", 0)) >= canvas_width or int(bounds.get("y", 0)) >= canvas_height:
                    hard.append({"code": "text_invisible", "message": f"关键文字图层 {layer_id} 不在画布可见范围"})
            if metric.get("overflow") and layer.get("role") in {"headline", "price", "details"}:
                hard.append({"code": "critical_text_overflow", "message": f"关键文字图层 {layer_id} 无法放入文本框"})
    hard.extend(_platform_hard_failures(design, platform, placement, merchant_info))
    return {"warnings": warnings, "hard_failures": hard, "checked": True}
