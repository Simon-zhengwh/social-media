from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat

from app.config import BACKEND_DIR, Settings

SANS_FONT_PATH = BACKEND_DIR / "fonts" / "NotoSansSC-VF.ttf"
BRUSH_FONT_PATH = BACKEND_DIR / "fonts" / "MaShanZheng-Regular.ttf"

WEIGHT_NAMES = {
    100: "Thin",
    300: "Light",
    350: "DemiLight",
    400: "Regular",
    500: "Medium",
    700: "Bold",
    900: "Black",
}


@dataclass
class DesignResult:
    image: Image.Image
    design: dict[str, Any] = field(default_factory=dict)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _parse_color(value: Any, fallback: str = "#FFFFFF") -> tuple[int, int, int, int]:
    if not isinstance(value, str):
        value = fallback
    text = value.strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", text):
        return (
            int(text[1:3], 16),
            int(text[3:5], 16),
            int(text[5:7], 16),
            255,
        )
    if re.fullmatch(r"#[0-9A-Fa-f]{8}", text):
        return (
            int(text[1:3], 16),
            int(text[3:5], 16),
            int(text[5:7], 16),
            int(text[7:9], 16),
        )
    return _parse_color(fallback, "#FFFFFF") if fallback != value else (255, 255, 255, 255)


def _load_font(
    size: int, weight: int = 700, family: str = "sans"
) -> ImageFont.FreeTypeFont:
    font_path = BRUSH_FONT_PATH if family == "brush" else SANS_FONT_PATH
    if not font_path.exists() and family == "brush":
        font_path = SANS_FONT_PATH
    if not font_path.exists():
        raise FileNotFoundError(f"字体文件不存在：{font_path}")
    font = ImageFont.truetype(str(font_path), max(6, int(size)))
    if family == "brush":
        return font
    nearest = min(WEIGHT_NAMES, key=lambda item: abs(item - int(weight)))
    try:
        font.set_variation_by_axes([nearest])
    except Exception:  # noqa: BLE001 - static fonts have no variation axis
        try:
            font.set_variation_by_name(WEIGHT_NAMES[nearest])
        except Exception:  # noqa: BLE001
            pass
    return font


def _measure(text: str, font: ImageFont.FreeTypeFont, spacing: int = 0) -> float:
    if not text:
        return 0.0
    return float(font.getlength(text)) + spacing * max(0, len(text) - 1)


def _tokens(text: str) -> list[str]:
    # Keep CJK as individual break opportunities, Latin words as one token.
    return re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*|\s+|.", text)


def _wrap(
    content: str,
    font: ImageFont.FreeTypeFont,
    max_width: float,
    spacing: int,
) -> list[str]:
    def split_long(token: str) -> list[str]:
        chunks: list[str] = []
        chunk = ""
        for char in token:
            if chunk and _measure(chunk + char, font, spacing) > max_width:
                chunks.append(chunk)
                chunk = char
            else:
                chunk += char
        if chunk:
            chunks.append(chunk)
        return chunks

    lines: list[str] = []
    for paragraph in content.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for token in _tokens(paragraph):
            if token.isspace() and not current:
                continue
            if _measure(token, font, spacing) > max_width:
                if current:
                    lines.append(current.rstrip())
                    current = ""
                chunks = split_long(token)
                lines.extend(chunks[:-1])
                current = chunks[-1] if chunks else ""
                continue
            candidate = current + token
            if current and _measure(candidate.rstrip(), font, spacing) > max_width:
                lines.append(current.rstrip())
                current = "" if token.isspace() else token
            else:
                current = candidate
        if current or not paragraph:
            lines.append(current.rstrip())
    return lines or [""]


def _fit_text(
    content: str,
    *,
    font_size: float,
    font_weight: int,
    font_family: str,
    box: dict[str, int],
    line_height: float,
    spacing: int,
) -> tuple[ImageFont.FreeTypeFont, list[str], int, bool, float]:
    initial = max(12, int(font_size))
    minimum = max(12, int(initial * 0.35))
    size = initial
    while True:
        font = _load_font(size, font_weight, font_family)
        lines = _wrap(content, font, box["width"], spacing)
        max_line = max(_measure(line, font, spacing) for line in lines) if lines else 0
        total_height = len(lines) * size * line_height
        if max_line <= box["width"] + 1 and total_height <= box["height"] + 1:
            return font, lines, size, False, total_height
        if size <= minimum:
            return font, lines, size, True, total_height
        size = max(minimum, size - max(1, int(initial * 0.04)))


def _line_x(box: dict[str, int], width: float, align: str) -> float:
    if align == "center":
        return box["x"] + (box["width"] - width) / 2
    if align == "right":
        return box["x"] + box["width"] - width
    return float(box["x"])


def _draw_spaced_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int, int],
    spacing: int,
    stroke_width: int = 0,
    stroke_fill: tuple[int, int, int, int] | None = None,
) -> None:
    x, y = xy
    if spacing == 0:
        draw.text(
            (x, y),
            text,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
        return
    for char in text:
        draw.text(
            (x, y),
            char,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
        x += float(font.getlength(char)) + spacing


def _text_block(
    layer: dict[str, Any],
    canvas_size: tuple[int, int],
) -> tuple[Image.Image, dict[str, Any]]:
    canvas_width, canvas_height = canvas_size
    content = str(layer.get("content") or "")
    style = layer.get("style") or {}
    position = layer.get("position") or {"x": 0, "y": 0}
    box = {
        "x": int(_clamp(float(position.get("x", 0)), 0, canvas_width - 10)),
        "y": int(_clamp(float(position.get("y", 0)), 0, canvas_height - 10)),
    }
    size = layer.get("size") or {}
    box["width"] = int(
        _clamp(float(size.get("width", canvas_width - box["x"])), 20, canvas_width - box["x"])
    )
    box["height"] = int(
        _clamp(
            float(size.get("height", canvas_height - box["y"])),
            20,
            canvas_height - box["y"],
        )
    )
    font_size = float(style.get("font_size") or 72)
    font_weight = int(style.get("font_weight") or 700)
    font_family = str(style.get("font_family") or "sans")
    line_height = float(style.get("line_height") or 1.08)
    spacing = int(style.get("letter_spacing") or 0)
    font, lines, used_size, overflow, block_height = _fit_text(
        content,
        font_size=font_size,
        font_weight=font_weight,
        font_family=font_family,
        box=box,
        line_height=line_height,
        spacing=spacing,
    )
    max_line = max((_measure(line, font, spacing) for line in lines), default=0)
    vertical = style.get("vertical_align") or "top"
    top = float(box["y"])
    if vertical == "middle":
        top = box["y"] + (box["height"] - block_height) / 2
    elif vertical == "bottom":
        top = box["y"] + box["height"] - block_height

    layer_image = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer_image)
    plate = style.get("background")
    if isinstance(plate, dict):
        padding = int(plate.get("padding") or 0)
        radius = int(plate.get("radius") or 0)
        x0 = _line_x(box, max_line, str(style.get("align") or "left")) - padding
        y0 = top - padding
        draw.rounded_rectangle(
            (
                max(0, x0),
                max(0, y0),
                min(canvas_width - 1, x0 + max_line + padding * 2),
                min(canvas_height - 1, y0 + block_height + padding * 2),
            ),
            radius=radius,
            fill=_parse_color(plate.get("color"), "#000000AA"),
        )

    fill = _parse_color(style.get("color"), "#FFFFFF")
    stroke = style.get("stroke") or {}
    stroke_width = int(stroke.get("width") or 0)
    stroke_color = _parse_color(stroke.get("color"), "#000000") if stroke_width else None
    shadow = style.get("shadow") or {}
    shadow_blur = int(shadow.get("blur") or 0)
    shadow_color = _parse_color(shadow.get("color"), "#00000000")
    if shadow_blur > 0 and shadow_color[3] > 0:
        shadow_layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow_layer)
        line_y = top
        for line in lines:
            line_width = _measure(line, font, spacing)
            _draw_spaced_text(
                shadow_draw,
                (
                    _line_x(box, line_width, str(style.get("align") or "left"))
                    + int(shadow.get("offset_x") or 0),
                    line_y + int(shadow.get("offset_y") or 0),
                ),
                line,
                font,
                shadow_color,
                0,
                stroke_width,
                shadow_color,
            )
            line_y += used_size * line_height
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(shadow_blur))
        layer_image.alpha_composite(shadow_layer)
        draw = ImageDraw.Draw(layer_image)

    runs = style.get("runs")
    run_text = "".join(str(item.get("text") or "") for item in runs) if isinstance(runs, list) else ""
    if isinstance(runs, list) and runs and "\n" not in content and run_text == content:
        total_width = _measure(content, font, spacing)
        cursor_x = _line_x(box, total_width, str(style.get("align") or "left"))
        for run in runs:
            run_text_value = str(run.get("text") or "")
            _draw_spaced_text(
                draw,
                (cursor_x, top),
                run_text_value,
                font,
                _parse_color(run.get("color"), style.get("color") or "#FFFFFF"),
                spacing,
                stroke_width,
                stroke_color,
            )
            cursor_x += _measure(run_text_value, font, spacing)
    else:
        line_y = top
        for line in lines:
            line_width = _measure(line, font, spacing)
            _draw_spaced_text(
                draw,
                (_line_x(box, line_width, str(style.get("align") or "left")), line_y),
                line,
                font,
                fill,
                spacing,
                stroke_width,
                stroke_color,
            )
            line_y += used_size * line_height

    opacity = float(_clamp(float(style.get("opacity", 1.0)), 0.05, 1.0))
    if opacity < 1.0:
        alpha = layer_image.getchannel("A").point(lambda value: int(value * opacity))
        layer_image.putalpha(alpha)

    metrics = {
        "id": layer.get("id"),
        "box": box,
        "used_font_size": used_size,
        "font_family": font_family,
        "lines": lines,
        "line_count": len(lines),
        "overflow": overflow,
        "bounds": {
            "x": int(_line_x(box, max_line, str(style.get("align") or "left"))),
            "y": int(top),
            "width": int(max_line),
            "height": int(block_height),
        },
        "rotation": float(layer.get("rotation") or 0),
    }
    rotation = float(layer.get("rotation") or 0)
    if abs(rotation) > 0.01:
        bounds = metrics["bounds"]
        padding = 60
        crop_box = (
            max(0, int(bounds["x"]) - padding),
            max(0, int(bounds["y"]) - padding),
            min(canvas_width, int(bounds["x"]) + int(bounds["width"]) + padding),
            min(canvas_height, int(bounds["y"]) + int(bounds["height"]) + padding),
        )
        if crop_box[2] > crop_box[0] and crop_box[3] > crop_box[1]:
            crop = layer_image.crop(crop_box)
            rotated = crop.rotate(
                -rotation,
                expand=True,
                resample=Image.Resampling.BICUBIC,
            )
            centered = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
            center_x = (crop_box[0] + crop_box[2]) / 2
            center_y = (crop_box[1] + crop_box[3]) / 2
            centered.alpha_composite(
                rotated,
                (
                    int(center_x - rotated.width / 2),
                    int(center_y - rotated.height / 2),
                ),
            )
            layer_image = centered
            metrics["rotated_bounds"] = {
                "x": int(center_x - rotated.width / 2),
                "y": int(center_y - rotated.height / 2),
                "width": rotated.width,
                "height": rotated.height,
            }
    return layer_image, metrics


def _logo_image(
    logo: Image.Image,
    *,
    position: dict[str, Any],
    size: dict[str, Any],
    opacity: float,
    rotation: float,
) -> tuple[Image.Image, dict[str, int]]:
    logo = logo.copy().convert("RGBA")
    target_width = max(1, int(size.get("width") or logo.width))
    target_height = max(1, int(size.get("height") or logo.height))
    logo.thumbnail((target_width, target_height), Image.Resampling.LANCZOS)
    if rotation:
        logo = logo.rotate(float(rotation), expand=True, resample=Image.Resampling.BICUBIC)
    if opacity < 1.0:
        alpha = logo.getchannel("A").point(lambda value: int(value * opacity))
        logo.putalpha(alpha)
    return logo, {
        "x": int(position.get("x", 0)),
        "y": int(position.get("y", 0)),
        "width": logo.width,
        "height": logo.height,
    }


def _image_element(
    source: Image.Image,
    *,
    position: dict[str, Any],
    size: dict[str, Any],
    opacity: float,
    rotation: float,
    fit: str,
    crop: dict[str, Any] | None,
) -> tuple[Image.Image, dict[str, int]]:
    image = source.copy().convert("RGBA")
    if isinstance(crop, dict):
        try:
            left = int(float(crop.get("x", 0)) * image.width)
            top = int(float(crop.get("y", 0)) * image.height)
            right = int((float(crop.get("x", 0)) + float(crop.get("width", 1))) * image.width)
            bottom = int((float(crop.get("y", 0)) + float(crop.get("height", 1))) * image.height)
            image = image.crop((max(0, left), max(0, top), min(image.width, right), min(image.height, bottom)))
        except (TypeError, ValueError):
            pass
    box_width = max(1, int(size.get("width") or image.width))
    box_height = max(1, int(size.get("height") or image.height))
    if fit == "cover":
        fitted = ImageOps.fit(image, (box_width, box_height), method=Image.Resampling.LANCZOS)
        offset_x = 0
        offset_y = 0
    else:
        fitted = image.copy()
        fitted.thumbnail((box_width, box_height), Image.Resampling.LANCZOS)
        offset_x = (box_width - fitted.width) // 2
        offset_y = (box_height - fitted.height) // 2
    if rotation:
        fitted = fitted.rotate(float(rotation), expand=True, resample=Image.Resampling.BICUBIC)
    if opacity < 1.0:
        alpha = fitted.getchannel("A").point(lambda value: int(value * opacity))
        fitted.putalpha(alpha)
    x = int(position.get("x", 0)) + offset_x
    y = int(position.get("y", 0)) + offset_y
    if rotation:
        x = int(position.get("x", 0)) + (box_width - fitted.width) // 2
        y = int(position.get("y", 0)) + (box_height - fitted.height) // 2
    return fitted, {"x": x, "y": y, "width": fitted.width, "height": fitted.height}


def _shape_element(layer: dict[str, Any]) -> tuple[Image.Image, dict[str, int]]:
    position = layer.get("position") or {}
    size = layer.get("size") or {}
    width = max(8, int(size.get("width") or 100))
    height = max(8, int(size.get("height") or 100))
    scale = 3
    canvas = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    fill = _parse_color(layer.get("fill"), "#00000000") if layer.get("fill") else None
    stroke = layer.get("stroke") or {}
    stroke_width = int(stroke.get("width") or 0) * scale
    stroke_color = _parse_color(stroke.get("color"), "#000000") if stroke_width else None
    shape = str(layer.get("shape") or "rect")
    box = (stroke_width, stroke_width, width * scale - stroke_width - 1, height * scale - stroke_width - 1)
    if shape == "ellipse":
        draw.ellipse(box, fill=fill, outline=stroke_color, width=stroke_width)
    elif shape == "polygon":
        points = layer.get("points") or []
        coords = [(float(p[0]) * width * scale, float(p[1]) * height * scale) for p in points if isinstance(p, (list, tuple)) and len(p) >= 2]
        if len(coords) >= 3:
            draw.polygon(coords, fill=fill)
            if stroke_color:
                draw.line(coords + [coords[0]], fill=stroke_color, width=max(1, stroke_width))
    elif shape == "line":
        points = layer.get("points") or []
        coords = [(float(p[0]) * width * scale, float(p[1]) * height * scale) for p in points if isinstance(p, (list, tuple)) and len(p) >= 2]
        if len(coords) >= 2:
            draw.line(coords, fill=stroke_color or fill or (255, 255, 255, 255), width=max(1, stroke_width or 2 * scale))
    else:
        radius = int(layer.get("radius") or 0) * scale
        if radius:
            draw.rounded_rectangle(box, radius=radius, fill=fill, outline=stroke_color, width=stroke_width)
        else:
            draw.rectangle(box, fill=fill, outline=stroke_color, width=stroke_width)
    canvas = canvas.resize((width, height), Image.Resampling.LANCZOS)
    opacity = float(_clamp(float(layer.get("opacity", 1.0)), 0.05, 1.0))
    if opacity < 1.0:
        alpha = canvas.getchannel("A").point(lambda value: int(value * opacity))
        canvas.putalpha(alpha)
    rotation = float(layer.get("rotation") or 0)
    if rotation:
        canvas = canvas.rotate(-rotation, expand=True, resample=Image.Resampling.BICUBIC)
    x = int(position.get("x", 0)) + (width - canvas.width) // 2
    y = int(position.get("y", 0)) + (height - canvas.height) // 2
    return canvas, {"x": x, "y": y, "width": canvas.width, "height": canvas.height}


def _component_element(
    layer: dict[str, Any],
    assets: dict[str, Image.Image],
) -> tuple[Image.Image, dict[str, int], list[dict[str, Any]]]:
    position = layer.get("position") or {}
    size = layer.get("size") or {}
    width = max(8, int(size.get("width") or 100))
    height = max(8, int(size.get("height") or 100))
    mode = str(layer.get("render_mode") or "native")
    compositing = layer.get("compositing") if isinstance(layer.get("compositing"), dict) else {}
    feather = float(compositing.get("feather_px") or 0)
    metrics: list[dict[str, Any]] = []
    if mode in {"flattened", "regeneratable"}:
        source = assets.get(str(layer.get("asset_id") or ""))
        if source is None:
            return Image.new("RGBA", (width, height), (0, 0, 0, 0)), {"x": int(position.get("x", 0)), "y": int(position.get("y", 0)), "width": width, "height": height}, [{"error": "asset_missing"}]
        image_element, box = _image_element(source, position={"x": 0, "y": 0}, size={"width": width, "height": height}, opacity=1.0, rotation=0.0, fit=str(layer.get("fit") or "contain"), crop=layer.get("crop"))
    else:
        local = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        for child in sorted(layer.get("children") or [], key=lambda item: int(item.get("z_index", 0))):
            if not isinstance(child, dict) or not child.get("visible", True):
                continue
            child_type = child.get("type")
            if child_type == "text":
                child_image, child_metrics = _text_block(child, (width, height))
                local.alpha_composite(child_image)
                metrics.append({"type": "text", **child_metrics})
            elif child_type == "shape":
                child_image, child_box = _shape_element(child)
                local.alpha_composite(child_image, (max(0, child_box["x"]), max(0, child_box["y"])))
                metrics.append({"type": "shape", "id": child.get("id"), "box": child_box})
            elif child_type in {"image", "logo"}:
                source = assets.get(str(child.get("asset_id") or ""))
                if source is None:
                    metrics.append({"type": "image", "id": child.get("id"), "error": "asset_missing"})
                    continue
                child_image, child_box = _image_element(source, position=child.get("position") or {}, size=child.get("size") or {}, opacity=float(child.get("opacity", 1.0)), rotation=float(child.get("rotation", 0.0)), fit=str(child.get("fit") or "contain"), crop=child.get("crop"))
                if feather > 0:
                    alpha = child_image.getchannel("A").filter(ImageFilter.GaussianBlur(feather))
                    child_image.putalpha(alpha)
                local.alpha_composite(child_image, (max(0, child_box["x"]), max(0, child_box["y"])))
                metrics.append({"type": "image", "id": child.get("id"), "box": child_box})
        image_element = local
        box = {"x": 0, "y": 0, "width": width, "height": height}
    if mode != "native" and feather > 0:
        alpha = image_element.getchannel("A").filter(ImageFilter.GaussianBlur(feather))
        image_element.putalpha(alpha)
    rotation = float(layer.get("rotation") or 0)
    if rotation:
        image_element = image_element.rotate(-rotation, expand=True, resample=Image.Resampling.BICUBIC)
    opacity_value = layer.get("opacity")
    if opacity_value is None:
        opacity_value = compositing.get("opacity", 1.0)
    opacity = float(_clamp(float(opacity_value), 0.05, 1.0))
    if opacity < 1.0:
        alpha = image_element.getchannel("A").point(lambda value: int(value * opacity))
        image_element.putalpha(alpha)
    x = int(position.get("x", 0)) + (width - image_element.width) // 2
    y = int(position.get("y", 0)) + (height - image_element.height) // 2
    return image_element, {"x": x, "y": y, "width": image_element.width, "height": image_element.height}, metrics


def _apply_color_match(
    canvas: Image.Image,
    image_element: Image.Image,
    x: int,
    y: int,
    amount: float,
) -> Image.Image:
    if amount <= 0:
        return image_element
    x = max(0, int(x))
    y = max(0, int(y))
    width = min(image_element.width, max(0, canvas.width - x))
    height = min(image_element.height, max(0, canvas.height - y))
    if width <= 0 or height <= 0:
        return image_element
    region = canvas.crop((x, y, x + width, y + height)).convert("RGB")
    stats = ImageStat.Stat(region.resize((1, 1), Image.Resampling.BOX))
    tint = tuple(int(channel) for channel in stats.mean[:3])
    rgb = image_element.convert("RGB")
    overlay = Image.new("RGB", rgb.size, tint)
    matched = Image.blend(rgb, overlay, float(_clamp(amount, 0.0, 0.35)))
    matched_rgba = matched.convert("RGBA")
    matched_rgba.putalpha(image_element.getchannel("A"))
    return matched_rgba


def _composite_with_mode(
    canvas: Image.Image,
    overlay: Image.Image,
    x: int,
    y: int,
    mode: str,
) -> None:
    x = max(0, int(x))
    y = max(0, int(y))
    if x >= canvas.width or y >= canvas.height:
        return
    width = min(overlay.width, canvas.width - x)
    height = min(overlay.height, canvas.height - y)
    if width <= 0 or height <= 0:
        return
    layer = overlay.crop((0, 0, width, height))
    if mode == "normal":
        canvas.alpha_composite(layer, (x, y))
        return
    region = canvas.crop((x, y, x + width, y + height))
    base_rgb = region.convert("RGB")
    layer_rgb = layer.convert("RGB")
    if mode == "multiply":
        blended = ImageChops.multiply(base_rgb, layer_rgb)
    elif mode == "screen":
        blended = ImageChops.screen(base_rgb, layer_rgb)
    elif mode == "soft_light":
        blended = ImageChops.soft_light(base_rgb, layer_rgb)
    else:
        canvas.alpha_composite(layer, (x, y))
        return
    blended_rgba = blended.convert("RGBA")
    blended_rgba.putalpha(layer.getchannel("A"))
    region.alpha_composite(blended_rgba)
    canvas.paste(region, (x, y))


def _component_shadow(
    canvas: Image.Image,
    image_element: Image.Image,
    x: int,
    y: int,
    shadow: dict[str, Any],
) -> None:
    alpha = image_element.getchannel("A")
    if alpha.getextrema()[1] == 0:
        return
    try:
        blur = int(_clamp(float(shadow.get("blur", 0)), 0, 80))
        offset_x = int(_clamp(float(shadow.get("offset_x", 0)), -80, 80))
        offset_y = int(_clamp(float(shadow.get("offset_y", 0)), -80, 80))
    except (TypeError, ValueError):
        return
    color = _parse_color(shadow.get("color"), "#00000040")
    shadow_layer = Image.new("RGBA", image_element.size, color[:3] + (0,))
    shadow_alpha = alpha.point(lambda value: int(value * (color[3] / 255.0)))
    shadow_layer.putalpha(shadow_alpha)
    if blur:
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(blur))
    canvas.alpha_composite(shadow_layer, (max(0, x + offset_x), max(0, y + offset_y)))


def render_design(
    settings: Settings,
    background: Image.Image,
    layers: list[dict[str, Any]],
    canvas_size: tuple[int, int],
    assets: dict[str, Image.Image] | None = None,
) -> DesignResult:
    canvas_width, canvas_height = int(canvas_size[0]), int(canvas_size[1])
    if background.size != (canvas_width, canvas_height):
        background = ImageOps.fit(
            background.convert("RGB"),
            (canvas_width, canvas_height),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
    canvas = background.convert("RGBA")
    assets = assets or {}
    metrics: list[dict[str, Any]] = []
    logo_box: dict[str, int] | None = None
    for layer in sorted(layers, key=lambda item: int(item.get("z_index", 0))):
        if not layer.get("visible", True):
            continue
        layer_type = layer.get("type")
        if layer_type == "background":
            continue
        if layer_type == "text":
            text_image, text_metrics = _text_block(layer, (canvas_width, canvas_height))
            canvas.alpha_composite(text_image)
            metrics.append({"type": "text", **text_metrics})
            continue
        if layer_type in {"logo", "image"}:
            hint = layer.get("render_hint") or ("baked" if layer.get("mode") == "model_integrated" else "overlay")
            if hint == "baked":
                metrics.append(
                    {"type": "image", "id": layer.get("id"), "skipped": "baked"}
                )
                continue
            asset_id = str(layer.get("asset_id") or "")
            source = assets.get(asset_id)
            if source is None:
                metrics.append(
                    {"type": "image", "id": layer.get("id"), "skipped": "asset_missing"}
                )
                continue
            image_element, box = _image_element(
                source,
                position=layer.get("position") or {},
                size=layer.get("size") or {},
                opacity=float(layer.get("opacity", 1.0)),
                rotation=float(layer.get("rotation", 0.0)),
                fit=str(layer.get("fit") or "contain"),
                crop=layer.get("crop"),
            )
            canvas.alpha_composite(image_element, (max(0, box["x"]), max(0, box["y"])))
            metrics.append({"type": "image", "id": layer.get("id"), "role": layer.get("role"), "box": box})
            if layer.get("role") == "logo" or layer_type == "logo":
                logo_box = box
            continue
        if layer_type == "shape":
            shape_image, shape_box = _shape_element(layer)
            canvas.alpha_composite(shape_image, (max(0, shape_box["x"]), max(0, shape_box["y"])))
            metrics.append({"type": "shape", "id": layer.get("id"), "box": shape_box})
            continue
        if layer_type == "visual_component":
            component_image, component_box, child_metrics = _component_element(layer, assets)
            compositing = layer.get("compositing") if isinstance(layer.get("compositing"), dict) else {}
            component_image = _apply_color_match(
                canvas,
                component_image,
                max(0, component_box["x"]),
                max(0, component_box["y"]),
                float(compositing.get("color_match") or 0),
            )
            shadow = compositing.get("shadow")
            if isinstance(shadow, dict):
                _component_shadow(
                    canvas,
                    component_image,
                    max(0, component_box["x"]),
                    max(0, component_box["y"]),
                    shadow,
                )
            _composite_with_mode(
                canvas,
                component_image,
                max(0, component_box["x"]),
                max(0, component_box["y"]),
                str(compositing.get("blend_mode") or "normal"),
            )
            metrics.append({
                "type": "visual_component",
                "id": layer.get("id"),
                "box": component_box,
                "children": child_metrics,
                "compositing": {
                    "blend_mode": compositing.get("blend_mode") or "normal",
                    "feather_px": compositing.get("feather_px") or 0,
                },
            })
            continue

    return DesignResult(
        image=canvas.convert("RGB"),
        design={
            "canvas": {"width": canvas_width, "height": canvas_height},
            "layers": metrics,
            "logo_box": logo_box,
        },
    )


def compose_poster(
    settings: Settings,
    background: Image.Image,
    logo: Image.Image | None,
    canvas_size: tuple[int, int] | None = None,
) -> DesignResult:
    """Legacy entry point. Logo is now a pure transparent overlay, never a badge."""
    canvas_size = canvas_size or (settings.canvas_width, settings.canvas_height)
    canvas_width, canvas_height = canvas_size
    layers: list[dict[str, Any]] = []
    assets: dict[str, Image.Image] = {}
    if logo is not None:
        logo_size = max(160, round(min(canvas_size) * 0.215))
        margin = max(24, round(logo_size * 0.18))
        layers.append(
            {
                "id": "logo_brand",
                "type": "logo",
                "asset_id": "legacy_logo",
                "position": {"x": margin, "y": margin},
                "size": {"width": logo_size, "height": logo_size},
                "opacity": 1.0,
                "rotation": 0,
                "mode": "overlay",
                "z_index": 10,
                "visible": True,
            }
        )
        assets["legacy_logo"] = logo
    result = render_design(
        settings,
        background,
        layers,
        (canvas_width, canvas_height),
        assets=assets,
    )
    if result.design.get("logo_box"):
        result.design["logo_box"]["width"] = logo_size
        result.design["logo_box"]["height"] = logo_size
    return result
