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
