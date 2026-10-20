from __future__ import annotations

import copy
import re
from typing import Any

SCHEMA_VERSION = 2

TEXT_ROLES = {
    "headline",
    "subheadline",
    "price",
    "offer",
    "details",
    "cta",
    "brand",
    "legal",
}

FONT_WEIGHTS = (100, 300, 400, 500, 700, 900)

DEFAULT_TEXT_STYLE: dict[str, Any] = {
    "font_family": "sans",
    "font_weight": 700,
    "font_size": 72,
    "color": "#FFFFFF",
    "align": "left",
    "vertical_align": "top",
    "line_height": 1.08,
    "letter_spacing": 0,
    "opacity": 1.0,
    "stroke": {"width": 0, "color": "#000000"},
    "shadow": {"blur": 0, "offset_x": 0, "offset_y": 0, "color": "#00000000"},
    "background": None,
}


class DesignError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def parse_color(value: Any, fallback: str = "#FFFFFF") -> str:
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", text) or re.fullmatch(
            r"#[0-9A-Fa-f]{8}", text
        ):
            return text.upper()
    return fallback


def normalize_style(raw: dict[str, Any] | None, *, font_size: float = 72) -> dict[str, Any]:
    style = copy.deepcopy(DEFAULT_TEXT_STYLE)
    style["font_size"] = int(_clamp(float(font_size), 12, 400))
    if not isinstance(raw, dict):
        return style

    weight = raw.get("font_weight", style["font_weight"])
    try:
        weight = int(weight)
    except (TypeError, ValueError):
        weight = style["font_weight"]
    style["font_weight"] = min(FONT_WEIGHTS, key=lambda item: abs(item - weight))

    try:
        style["font_size"] = int(_clamp(float(raw.get("font_size", style["font_size"])), 12, 400))
    except (TypeError, ValueError):
        pass
    try:
        style["line_height"] = float(_clamp(float(raw.get("line_height", 1.08)), 0.7, 2.2))
    except (TypeError, ValueError):
        pass
    try:
        style["letter_spacing"] = int(_clamp(float(raw.get("letter_spacing", 0)), -20, 60))
    except (TypeError, ValueError):
        pass
    try:
        style["opacity"] = float(_clamp(float(raw.get("opacity", 1.0)), 0.05, 1.0))
    except (TypeError, ValueError):
        pass

    style["color"] = parse_color(raw.get("color"), style["color"])
    family = raw.get("font_family")
    style["font_family"] = family if family in {"sans", "brush"} else "sans"
    align = raw.get("align")
    style["align"] = align if align in {"left", "center", "right"} else "left"
    vertical = raw.get("vertical_align")
    style["vertical_align"] = (
        vertical if vertical in {"top", "middle", "bottom"} else "top"
    )

    stroke = raw.get("stroke") if isinstance(raw.get("stroke"), dict) else {}
    try:
        stroke_width = int(_clamp(float(stroke.get("width", 0)), 0, 30))
    except (TypeError, ValueError):
        stroke_width = 0
    style["stroke"] = {
        "width": stroke_width,
        "color": parse_color(stroke.get("color"), "#000000"),
    }

    shadow = raw.get("shadow") if isinstance(raw.get("shadow"), dict) else {}
    try:
        blur = int(_clamp(float(shadow.get("blur", 0)), 0, 60))
        offset_x = int(_clamp(float(shadow.get("offset_x", 0)), -60, 60))
        offset_y = int(_clamp(float(shadow.get("offset_y", 0)), -60, 60))
    except (TypeError, ValueError):
        blur, offset_x, offset_y = 0, 0, 0
    style["shadow"] = {
        "blur": blur,
        "offset_x": offset_x,
        "offset_y": offset_y,
        "color": parse_color(shadow.get("color"), "#00000000"),
    }

    plate = raw.get("background")
    if isinstance(plate, dict):
        try:
            radius = int(_clamp(float(plate.get("radius", 20)), 0, 120))
            padding = int(_clamp(float(plate.get("padding", 16)), 0, 120))
        except (TypeError, ValueError):
            radius, padding = 20, 16
        style["background"] = {
            "color": parse_color(plate.get("color"), "#000000AA"),
            "radius": radius,
            "padding": padding,
        }
    else:
        style["background"] = None
    return style


def make_text_layer(
    *,
    layer_id: str,
    name: str,
    role: str,
    content: str,
    box: dict[str, Any],
    style: dict[str, Any] | None = None,
    z_index: int = 20,
    rotation: float = 0.0,
) -> dict[str, Any]:
    role = role if role in TEXT_ROLES else "headline"
    x = int(max(0, float(box.get("x", 0))))
    y = int(max(0, float(box.get("y", 0))))
    width = int(max(20, float(box.get("width", 600))))
    height = int(max(20, float(box.get("height", 160))))
    return {
        "id": layer_id,
        "type": "text",
        "name": name,
        "role": role,
        "content": str(content or ""),
        "position": {"x": x, "y": y},
        "size": {"width": width, "height": height},
        "style": normalize_style(style),
        "rotation": float(_clamp(float(rotation), -20.0, 20.0)),
        "z_index": int(z_index),
        "visible": True,
        "locked": False,
    }


def make_logo_layer(
    *,
    layer_id: str,
    asset_id: str,
    box: dict[str, Any],
    mode: str = "model_integrated",
    z_index: int = 40,
) -> dict[str, Any]:
    x = int(max(0, float(box.get("x", 0))))
    y = int(max(0, float(box.get("y", 0))))
    width = int(max(16, float(box.get("width", 150))))
    height = int(max(16, float(box.get("height", 150))))
    return {
        "id": layer_id,
        "type": "logo",
        "name": "品牌 Logo",
        "asset_id": asset_id,
        "position": {"x": x, "y": y},
        "size": {"width": width, "height": height},
        "opacity": 1.0,
        "rotation": 0,
        "mode": mode if mode in {"model_integrated", "overlay"} else "overlay",
        "z_index": int(z_index),
        "visible": True,
        "locked": False,
    }


def make_design(
    *,
    canvas: dict[str, int],
    clean_asset_id: str,
    render_asset_id: str,
    background_prompt: str,
    logo_integrated: bool,
    text_layers: list[dict[str, Any]],
    logo_layer: dict[str, Any] | None,
) -> dict[str, Any]:
    layers: list[dict[str, Any]] = [
        {
            "id": "background",
            "type": "background",
            "name": "场景底图",
            "asset_id": render_asset_id,
            "z_index": 0,
            "visible": True,
            "locked": True,
        }
    ]
    layers.extend(text_layers)
    if logo_layer is not None:
        layers.append(logo_layer)
    for index, layer in enumerate(layers):
        layer["z_index"] = index * 10
    design = {
        "schema_version": SCHEMA_VERSION,
        "canvas": {
            "width": int(canvas["width"]),
            "height": int(canvas["height"]),
        },
        "background": {
            "clean_asset_id": clean_asset_id,
            "render_asset_id": render_asset_id,
            "prompt": background_prompt,
            "logo_integrated": bool(logo_integrated),
        },
        "layers": layers,
    }
    validate_design(design)
    return design


def validate_design(design: dict[str, Any]) -> None:
    if not isinstance(design, dict):
        raise DesignError("invalid_design", "设计状态不是对象")
    canvas = design.get("canvas")
    if not isinstance(canvas, dict):
        raise DesignError("invalid_design", "缺少画布信息")
    width, height = int(canvas.get("width", 0)), int(canvas.get("height", 0))
    if width <= 0 or height <= 0:
        raise DesignError("invalid_design", "画布尺寸无效")
    layers = design.get("layers")
    if not isinstance(layers, list) or not layers:
        raise DesignError("invalid_design", "设计状态缺少图层")
    ids: set[str] = set()
    background_count = 0
    for layer in layers:
        if not isinstance(layer, dict):
            raise DesignError("invalid_design", "图层格式无效")
        layer_id = layer.get("id")
        if not isinstance(layer_id, str) or not layer_id:
            raise DesignError("invalid_design", "图层缺少 id")
        if layer_id in ids:
            raise DesignError("invalid_design", f"图层 id 重复：{layer_id}")
        ids.add(layer_id)
        layer_type = layer.get("type")
        if layer_type not in {"background", "text", "logo"}:
            raise DesignError("invalid_design", f"未知图层类型：{layer_type}")
        if layer_type == "background":
            background_count += 1
            if not layer.get("asset_id"):
                raise DesignError("invalid_design", "底图图层缺少素材")
        if layer_type == "text":
            if not isinstance(layer.get("content"), str):
                raise DesignError("invalid_design", f"文字图层 {layer_id} 内容无效")
            position = layer.get("position") or {}
            size = layer.get("size") or {}
            for key in ("x", "y"):
                if float(position.get(key, -1)) < 0:
                    raise DesignError("invalid_design", f"文字图层 {layer_id} 坐标无效")
            if float(size.get("width", 0)) <= 0 or float(size.get("height", 0)) <= 0:
                raise DesignError("invalid_design", f"文字图层 {layer_id} 尺寸无效")
        if layer_type == "logo" and not layer.get("asset_id"):
            raise DesignError("invalid_design", f"Logo 图层 {layer_id} 缺少素材")
    if background_count != 1:
        raise DesignError("invalid_design", "必须且只能有一个底图图层")


def sorted_layers(design: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(design.get("layers", []), key=lambda item: int(item.get("z_index", 0)))


def find_layer(design: dict[str, Any], layer_id: str) -> dict[str, Any]:
    for layer in design.get("layers", []):
        if layer.get("id") == layer_id:
            return layer
    raise DesignError("layer_not_found", f"图层不存在：{layer_id}")


def find_layer_by_role(design: dict[str, Any], role: str) -> dict[str, Any] | None:
    for layer in design.get("layers", []):
        if layer.get("type") == "text" and layer.get("role") == role:
            return layer
    return None


def text_layers(design: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        layer
        for layer in sorted_layers(design)
        if layer.get("type") == "text" and layer.get("content", "").strip()
    ]


def logo_layer(design: dict[str, Any]) -> dict[str, Any] | None:
    for layer in design.get("layers", []):
        if layer.get("type") == "logo":
            return layer
    return None


def apply_layer_patch(layer: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Return a patched copy. Only whitelisted fields of the given layer type change."""
    if not isinstance(patch, dict):
        raise DesignError("invalid_patch", "修改内容必须是对象")
    updated = copy.deepcopy(layer)
    layer_type = updated.get("type")

    allowed_common = {"visible"}
    if layer_type == "text":
        allowed = allowed_common | {"content", "style", "position", "size", "rotation"}
    elif layer_type == "logo":
        allowed = allowed_common | {
            "position",
            "size",
            "opacity",
            "rotation",
            "asset_id",
            "mode",
        }
    elif layer_type == "background":
        allowed = set()
    else:
        raise DesignError("invalid_layer", "未知图层类型")

    unknown = set(patch) - allowed
    if unknown:
        raise DesignError(
            "field_not_allowed",
            f"{layer_type} 图层不允许修改字段：{', '.join(sorted(unknown))}",
        )

    if "content" in patch and layer_type == "text":
        content = patch["content"]
        if not isinstance(content, str):
            raise DesignError("invalid_content", "文字内容必须是字符串")
        if len(content) > 500:
            raise DesignError("content_too_long", "文字内容不能超过 500 字")
        updated["content"] = content
    if "style" in patch and layer_type == "text":
        style_patch = copy.deepcopy(patch["style"])
        if not isinstance(style_patch, dict):
            raise DesignError("invalid_style", "样式必须是对象")
        merged = copy.deepcopy(updated.get("style") or {})
        for key in ("stroke", "shadow", "background"):
            if key in style_patch and isinstance(style_patch[key], dict) and isinstance(
                merged.get(key), dict
            ):
                merged[key] = {**merged[key], **style_patch[key]}
            elif key in style_patch:
                merged[key] = style_patch[key]
            style_patch.pop(key, None)
        merged.update(style_patch)
        updated["style"] = normalize_style(merged)
    if "position" in patch:
        position = patch["position"]
        if not isinstance(position, dict):
            raise DesignError("invalid_position", "位置必须是对象")
        x = float(position.get("x", updated["position"]["x"]))
        y = float(position.get("y", updated["position"]["y"]))
        updated["position"] = {"x": int(max(0, x)), "y": int(max(0, y))}
    if "size" in patch:
        size = patch["size"]
        if not isinstance(size, dict):
            raise DesignError("invalid_size", "尺寸必须是对象")
        width = float(size.get("width", updated.get("size", {}).get("width", 100)))
        height = float(size.get("height", updated.get("size", {}).get("height", 100)))
        if width <= 0 or height <= 0:
            raise DesignError("invalid_size", "尺寸必须大于 0")
        updated["size"] = {"width": int(max(16, width)), "height": int(max(16, height))}
    if "visible" in patch:
        updated["visible"] = bool(patch["visible"])
    if "rotation" in patch and layer_type == "text":
        updated["rotation"] = float(_clamp(float(patch["rotation"]), -20.0, 20.0))
    if "opacity" in patch and layer_type == "logo":
        updated["opacity"] = float(_clamp(float(patch["opacity"]), 0.05, 1.0))
    if "rotation" in patch and layer_type == "logo":
        updated["rotation"] = float(_clamp(float(patch["rotation"]), -180, 180))
    if "asset_id" in patch and layer_type == "logo":
        asset_id = patch["asset_id"]
        if not isinstance(asset_id, str) or not asset_id:
            raise DesignError("invalid_asset", "Logo 素材 id 无效")
        updated["asset_id"] = asset_id
    if "mode" in patch and layer_type == "logo":
        mode = patch["mode"]
        if mode not in {"model_integrated", "overlay"}:
            raise DesignError("invalid_mode", "Logo 模式无效")
        updated["mode"] = mode
    return updated


def replace_layer(design: dict[str, Any], updated: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(design)
    for index, layer in enumerate(result.get("layers", [])):
        if layer.get("id") == updated.get("id"):
            result["layers"][index] = copy.deepcopy(updated)
            return result
    raise DesignError("layer_not_found", f"图层不存在：{updated.get('id')}")


def design_summary(design: dict[str, Any]) -> dict[str, Any]:
    """Compact state sent to the editing model. Contains no internal asset paths."""
    layers = []
    for layer in sorted_layers(design):
        item: dict[str, Any] = {
            "id": layer.get("id"),
            "type": layer.get("type"),
            "name": layer.get("name"),
            "visible": layer.get("visible", True),
            "position": layer.get("position"),
            "size": layer.get("size"),
        }
        if layer.get("type") == "text":
            item["role"] = layer.get("role")
            item["content"] = layer.get("content")
            item["rotation"] = layer.get("rotation", 0)
            style = layer.get("style") or {}
            item["style"] = {
                "font_size": style.get("font_size"),
                "font_weight": style.get("font_weight"),
                "color": style.get("color"),
                "align": style.get("align"),
            }
        if layer.get("type") == "logo":
            item["mode"] = layer.get("mode")
            item["opacity"] = layer.get("opacity")
        layers.append(item)
    return {
        "canvas": design.get("canvas"),
        "background": {
            "has_logo": bool(logo_layer(design)),
            "logo_integrated": bool((design.get("background") or {}).get("logo_integrated")),
        },
        "layers": layers,
    }


def copy_from_design(design: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for layer in text_layers(design):
        role = str(layer.get("role") or "")
        if role and role not in result:
            result[role] = str(layer.get("content") or "")
    return result


def scale_box(box: dict[str, Any], scale: float) -> dict[str, int]:
    return {
        "width": max(16, int(float(box.get("width", 100)) * scale)),
        "height": max(16, int(float(box.get("height", 100)) * scale)),
    }
