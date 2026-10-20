from __future__ import annotations

import copy
import re
from typing import Any

SCHEMA_VERSION = 3
RENDERER_BUILD = "pillow-scene-v3.1"

VISUAL_COMPONENT_TYPES = {"headline", "promotion_price", "offer_badge", "activity_info", "brand_lockup"}
RENDER_MODES = {"native", "regeneratable", "flattened"}

TEXT_ROLES = {
    "headline",
    "subheadline",
    "price",
    "offer",
    "details",
    "cta",
    "brand",
    "legal",
    "caption",
}
IMAGE_ROLES = {"logo", "product", "decoration"}
SHAPE_TYPES = {"rect", "ellipse", "polygon", "line"}
FONT_WEIGHTS = (100, 300, 400, 500, 700, 900)

SUPPORTED_CAPABILITIES = (
    "text.basic",
    "text.rotation",
    "image.alpha",
    "image.cover",
    "image.contain",
    "shape.rect",
    "shape.ellipse",
    "shape.polygon",
    "shape.line",
    "component.compositing",
    "component.contextual",
)

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
    "runs": None,
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
    style["vertical_align"] = vertical if vertical in {"top", "middle", "bottom"} else "top"
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
    runs = raw.get("runs")
    if isinstance(runs, list):
        normalized_runs = []
        for item in runs[:8]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "")
            if not text:
                continue
            normalized_runs.append({
                "text": text,
                "color": parse_color(item.get("color"), style["color"]),
            })
        style["runs"] = normalized_runs or None
    else:
        style["runs"] = None
    return style


def _default_edit_policy(*, created_by: str = "agent") -> dict[str, Any]:
    return {"ai_locked": False, "manual_edited": False, "created_by": created_by}


def _normalize_geometry(box: dict[str, Any], *, minimum: int = 16) -> tuple[dict[str, int], dict[str, int]]:
    try:
        x = int(float(box.get("x", 0)))
        y = int(float(box.get("y", 0)))
        width = int(max(minimum, float(box.get("width", 600))))
        height = int(max(minimum, float(box.get("height", 160))))
    except (TypeError, ValueError):
        x, y, width, height = 0, 0, 600, 160
    return {"x": x, "y": y}, {"width": width, "height": height}


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
    copy_id: str | None = None,
) -> dict[str, Any]:
    role = role if role in TEXT_ROLES else "headline"
    position, size = _normalize_geometry(box, minimum=20)
    layer = {
        "id": layer_id,
        "type": "text",
        "name": name,
        "role": role,
        "content": str(content or ""),
        "position": position,
        "size": size,
        "style": normalize_style(style, font_size=float((style or {}).get("font_size", 72))),
        "rotation": float(_clamp(float(rotation or 0), -45.0, 45.0)),
        "z_index": int(z_index),
        "visible": True,
        "locked": False,
        "edit_policy": _default_edit_policy(),
        "provenance": {"created_by": "agent"},
    }
    if copy_id:
        layer["copy_id"] = str(copy_id)
    return layer


def _normalize_crop(raw: Any) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    try:
        x = float(_clamp(float(raw.get("x", 0)), 0, 1))
        y = float(_clamp(float(raw.get("y", 0)), 0, 1))
        width = float(_clamp(float(raw.get("width", 1)), 0.01, 1 - x))
        height = float(_clamp(float(raw.get("height", 1)), 0.01, 1 - y))
    except (TypeError, ValueError):
        return None
    return {"x": x, "y": y, "width": width, "height": height}


def make_image_layer(
    *,
    layer_id: str,
    name: str,
    asset_id: str,
    role: str = "decoration",
    box: dict[str, Any] | None = None,
    z_index: int = 40,
    opacity: float = 1.0,
    rotation: float = 0.0,
    fit: str = "contain",
    crop: dict[str, Any] | None = None,
    render_hint: str = "overlay",
) -> dict[str, Any]:
    position, size = _normalize_geometry(box or {}, minimum=16)
    return {
        "id": layer_id,
        "type": "image",
        "name": name,
        "role": role if role in IMAGE_ROLES else "decoration",
        "asset_id": str(asset_id),
        "position": position,
        "size": size,
        "opacity": float(_clamp(float(opacity), 0.05, 1.0)),
        "rotation": float(_clamp(float(rotation or 0), -180.0, 180.0)),
        "fit": fit if fit in {"contain", "cover"} else "contain",
        "crop": _normalize_crop(crop),
        "render_hint": render_hint if render_hint in {"overlay", "baked"} else "overlay",
        "z_index": int(z_index),
        "visible": True,
        "locked": False,
        "edit_policy": _default_edit_policy(),
        "provenance": {"created_by": "agent"},
    }


def make_logo_layer(
    *,
    layer_id: str,
    asset_id: str,
    box: dict[str, Any],
    mode: str = "model_integrated",
    z_index: int = 40,
) -> dict[str, Any]:
    hint = "baked" if mode == "model_integrated" else "overlay"
    layer = make_image_layer(
        layer_id=layer_id,
        name="品牌 Logo",
        asset_id=asset_id,
        role="logo",
        box=box,
        z_index=z_index,
        fit="contain",
        render_hint=hint,
    )
    layer["mode"] = mode if mode in {"model_integrated", "overlay"} else "overlay"
    return layer


def make_shape_layer(
    *,
    layer_id: str,
    name: str,
    shape: str,
    box: dict[str, Any],
    fill: str | None = "#FFFFFF",
    stroke: dict[str, Any] | None = None,
    opacity: float = 1.0,
    rotation: float = 0.0,
    z_index: int = 20,
    points: list[list[float]] | None = None,
    radius: int = 0,
) -> dict[str, Any]:
    position, size = _normalize_geometry(box, minimum=8)
    stroke_raw = stroke if isinstance(stroke, dict) else {}
    try:
        stroke_width = int(_clamp(float(stroke_raw.get("width", 0)), 0, 80))
    except (TypeError, ValueError):
        stroke_width = 0
    normalized_points: list[list[float]] = []
    for item in points or []:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            try:
                normalized_points.append([
                    float(_clamp(float(item[0]), -2, 3)),
                    float(_clamp(float(item[1]), -2, 3)),
                ])
            except (TypeError, ValueError):
                continue
    return {
        "id": layer_id,
        "type": "shape",
        "name": name,
        "shape": shape if shape in SHAPE_TYPES else "rect",
        "position": position,
        "size": size,
        "fill": parse_color(fill, "#00000000") if fill else None,
        "stroke": {
            "width": stroke_width,
            "color": parse_color(stroke_raw.get("color"), "#000000"),
        } if stroke_width else None,
        "opacity": float(_clamp(float(opacity), 0.05, 1.0)),
        "rotation": float(_clamp(float(rotation or 0), -180.0, 180.0)),
        "radius": int(_clamp(float(radius), 0, 500)),
        "points": normalized_points,
        "z_index": int(z_index),
        "visible": True,
        "locked": False,
        "edit_policy": _default_edit_policy(),
        "provenance": {"created_by": "agent"},
    }


def make_visual_component(
    *,
    component_id: str,
    name: str,
    semantic_type: str,
    content: dict[str, Any],
    children: list[dict[str, Any]],
    box: dict[str, Any],
    render_mode: str = "native",
    asset_id: str | None = None,
    z_index: int = 300,
    rotation: float = 0.0,
    style: dict[str, Any] | None = None,
    compositing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    position, size = _normalize_geometry(box, minimum=8)
    normalized_children = []
    for index, child in enumerate(children):
        item = copy.deepcopy(child)
        item.setdefault("id", f"{component_id}_child_{index + 1}")
        item.setdefault("z_index", (index + 1) * 10)
        normalized_children.append(item)
    component = {
        "id": component_id,
        "type": "visual_component",
        "name": name,
        "semantic_type": semantic_type if semantic_type in VISUAL_COMPONENT_TYPES else "headline",
        "render_mode": render_mode if render_mode in RENDER_MODES else "native",
        "content": copy.deepcopy(content),
        "children": normalized_children,
        "position": position,
        "size": size,
        "rotation": float(_clamp(float(rotation or 0), -180.0, 180.0)),
        "z_index": int(z_index),
        "visible": True,
        "locked": False,
        "style": copy.deepcopy(style or {}),
        "compositing": copy.deepcopy(compositing or {
            "blend_mode": "normal",
            "opacity": 1.0,
            "feather_px": 0,
            "shadow": None,
            "color_match": 0.0,
            "anchor": "scene",
        }),
        "edit_policy": {"ai_locked": False, "manual_edited": False, "created_by": "agent"},
        "provenance": {"created_by": "agent", "component_mode": render_mode},
    }
    if asset_id:
        component["asset_id"] = asset_id
    return component


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
    layers: list[dict[str, Any]] = list(text_layers)
    if logo_layer is not None:
        layers.append(logo_layer)
    return make_scene(
        canvas=canvas,
        clean_asset_id=clean_asset_id,
        render_asset_id=render_asset_id,
        background_prompt=background_prompt,
        logo_integrated=logo_integrated,
        layers=layers,
        design_intent=None,
    )


def make_scene(
    *,
    canvas: dict[str, int],
    clean_asset_id: str,
    render_asset_id: str,
    background_prompt: str,
    logo_integrated: bool,
    layers: list[dict[str, Any]],
    design_intent: dict[str, Any] | None = None,
    scene_revision: int = 1,
) -> dict[str, Any]:
    background = {
        "id": "background",
        "type": "background",
        "name": "场景底图",
        "asset_id": render_asset_id,
        "z_index": 0,
        "visible": True,
        "locked": True,
        "edit_policy": {"ai_locked": True, "manual_edited": False, "created_by": "agent"},
        "provenance": {"created_by": "agent"},
    }
    scene_layers = [background]
    for index, layer in enumerate(layers):
        item = copy.deepcopy(layer)
        item.setdefault("z_index", (index + 1) * 10)
        item.setdefault("edit_policy", _default_edit_policy())
        item.setdefault("provenance", {"created_by": "agent"})
        scene_layers.append(item)
    scene_layers = sorted(scene_layers, key=lambda item: (int(item.get("z_index", 0)), 0 if item.get("type") == "background" else 1))
    design = {
        "schema_version": SCHEMA_VERSION,
        "scene_revision": int(scene_revision),
        "canvas": {
            "width": int(canvas["width"]),
            "height": int(canvas["height"]),
            "color_space": "srgb",
        },
        "renderer": {"engine": "pillow", "build_id": RENDERER_BUILD},
        "required_capabilities": list(SUPPORTED_CAPABILITIES),
        "design_intent": normalize_design_intent(design_intent),
        "background": {
            "clean_asset_id": clean_asset_id,
            "render_asset_id": render_asset_id,
            "prompt": background_prompt,
            "logo_integrated": bool(logo_integrated),
        },
        "layers": scene_layers,
    }
    validate_design(design)
    return design


def normalize_design_intent(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    palette = []
    for item in raw.get("palette") or []:
        color = parse_color(item, "")
        if color:
            palette.append(color)
    return {
        "id": str(raw.get("id") or "intent_default")[:80],
        "hash": str(raw.get("hash") or "")[:80],
        "concept": str(raw.get("concept") or "品牌商业海报")[:500],
        "focal_point": str(raw.get("focal_point") or "主体与标题")[:500],
        "composition": str(raw.get("composition") or "根据需求自由构图")[:500],
        "palette": palette[:8],
        "typography": str(raw.get("typography") or "清晰标题与信息层级")[:500],
        "text_image_relation": str(raw.get("text_image_relation") or "图文互相配合")[:500],
        "decor_strategy": str(raw.get("decor_strategy") or "克制使用装饰")[:500],
        "density": str(raw.get("density") or "balanced")[:40],
    }


def upgrade_design(design: dict[str, Any], *, scene_revision: int | None = None) -> dict[str, Any]:
    if not isinstance(design, dict):
        raise DesignError("invalid_design", "设计状态不是对象")
    result = copy.deepcopy(design)
    if int(result.get("schema_version") or 0) >= 3:
        result.setdefault("scene_revision", int(scene_revision or 1))
        result.setdefault("renderer", {"engine": "pillow", "build_id": RENDERER_BUILD})
        result.setdefault("required_capabilities", list(SUPPORTED_CAPABILITIES))
        result.setdefault("design_intent", normalize_design_intent(None))
        result["canvas"].setdefault("color_space", "srgb")
        for layer in result.get("layers", []):
            layer.setdefault("edit_policy", _default_edit_policy())
            layer.setdefault("provenance", {"created_by": "agent"})
        validate_design(result)
        return result

    background = result.get("background") or {}
    layers: list[dict[str, Any]] = []
    for layer in result.get("layers", []):
        if not isinstance(layer, dict):
            continue
        item = copy.deepcopy(layer)
        if item.get("type") == "logo":
            mode = item.get("mode") or "overlay"
            image = make_image_layer(
                layer_id=str(item.get("id") or "logo_brand"),
                name=str(item.get("name") or "品牌 Logo"),
                asset_id=str(item.get("asset_id") or ""),
                role="logo",
                box={**item.get("position", {}), **item.get("size", {})},
                z_index=int(item.get("z_index", 40)),
                opacity=float(item.get("opacity", 1.0)),
                rotation=float(item.get("rotation", 0.0)),
                fit="contain",
                render_hint="baked" if mode == "model_integrated" else "overlay",
            )
            image["mode"] = mode
            image["visible"] = bool(item.get("visible", True))
            layers.append(image)
            continue
        if item.get("type") == "background":
            continue
        item.setdefault("edit_policy", _default_edit_policy())
        item.setdefault("provenance", {"created_by": "agent"})
        layers.append(item)
    return make_scene(
        canvas={"width": int(result["canvas"]["width"]), "height": int(result["canvas"]["height"])},
        clean_asset_id=str(background.get("clean_asset_id") or background.get("render_asset_id") or ""),
        render_asset_id=str(background.get("render_asset_id") or background.get("clean_asset_id") or ""),
        background_prompt=str(background.get("prompt") or ""),
        logo_integrated=bool(background.get("logo_integrated")),
        layers=layers,
        design_intent=result.get("design_intent"),
        scene_revision=int(scene_revision or result.get("scene_revision") or 1),
    )


def validate_design(design: dict[str, Any]) -> None:
    if not isinstance(design, dict):
        raise DesignError("invalid_design", "设计状态不是对象")
    canvas = design.get("canvas")
    if not isinstance(canvas, dict):
        raise DesignError("invalid_design", "缺少画布信息")
    width, height = int(canvas.get("width", 0)), int(canvas.get("height", 0))
    if width <= 0 or height <= 0:
        raise DesignError("invalid_design", "画布尺寸无效")
    caps = design.get("required_capabilities")
    if caps is not None:
        if not isinstance(caps, list):
            raise DesignError("invalid_capabilities", "required_capabilities 必须是数组")
        unknown = set(str(item) for item in caps) - set(SUPPORTED_CAPABILITIES)
        if unknown:
            raise DesignError("unsupported_capability", f"不支持的渲染能力：{', '.join(sorted(unknown))}")
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
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,80}", layer_id):
            raise DesignError("invalid_design", f"图层 id 不合法：{layer_id}")
        if layer_id in ids:
            raise DesignError("invalid_design", f"图层 id 重复：{layer_id}")
        ids.add(layer_id)
        layer_type = layer.get("type")
        if layer_type not in {"background", "text", "image", "shape", "logo", "visual_component"}:
            raise DesignError("invalid_design", f"未知图层类型：{layer_type}")
        if layer_type == "background":
            background_count += 1
            if not layer.get("asset_id"):
                raise DesignError("invalid_design", "底图图层缺少素材")
        if layer_type == "text":
            if not isinstance(layer.get("content"), str):
                raise DesignError("invalid_design", f"文字图层 {layer_id} 内容无效")
            if len(layer.get("content") or "") > 500:
                raise DesignError("content_too_long", f"文字图层 {layer_id} 超过 500 字")
            position = layer.get("position") or {}
            size = layer.get("size") or {}
            if not isinstance(position, dict):
                raise DesignError("invalid_design", f"文字图层 {layer_id} 坐标无效")
            if float(size.get("width", 0)) <= 0 or float(size.get("height", 0)) <= 0:
                raise DesignError("invalid_design", f"文字图层 {layer_id} 尺寸无效")
        if layer_type in {"image", "logo"} and not layer.get("asset_id"):
            raise DesignError("invalid_design", f"图片图层 {layer_id} 缺少素材")
        if layer_type == "image" and layer.get("fit") not in {None, "contain", "cover"}:
            raise DesignError("invalid_design", f"图片图层 {layer_id} fit 无效")
        if layer_type == "shape" and layer.get("shape") not in SHAPE_TYPES:
            raise DesignError("invalid_design", f"形状图层 {layer_id} 类型无效")
        if layer_type == "visual_component":
            mode = layer.get("render_mode") or "native"
            if mode not in RENDER_MODES:
                raise DesignError("invalid_design", f"视觉组件 {layer_id} render_mode 无效")
            if mode == "native" and not isinstance(layer.get("children"), list):
                raise DesignError("invalid_design", f"视觉组件 {layer_id} 缺少 children")
            if mode != "native" and not layer.get("asset_id"):
                raise DesignError("invalid_design", f"视觉组件 {layer_id} 缺少生成素材")
    if background_count != 1:
        raise DesignError("invalid_design", "必须且只能有一个底图图层")


def sorted_layers(design: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(design.get("layers", []), key=lambda item: int(item.get("z_index", 0)))


def find_layer(design: dict[str, Any], layer_id: str) -> dict[str, Any]:
    for layer in design.get("layers", []):
        if layer.get("id") == layer_id:
            return layer
    raise DesignError("layer_not_found", f"图层不存在：{layer_id}")


def find_layers_by_role(design: dict[str, Any], role: str) -> list[dict[str, Any]]:
    return [layer for layer in sorted_layers(design) if layer.get("role") == role]


def find_layer_by_role(design: dict[str, Any], role: str) -> dict[str, Any] | None:
    layers = find_layers_by_role(design, role)
    return layers[0] if layers else None


def text_layers(design: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        layer
        for layer in sorted_layers(design)
        if layer.get("type") == "text" and layer.get("content", "").strip()
    ]


def image_layers(design: dict[str, Any]) -> list[dict[str, Any]]:
    return [layer for layer in sorted_layers(design) if layer.get("type") == "image"]


def logo_layer(design: dict[str, Any]) -> dict[str, Any] | None:
    for layer in sorted_layers(design):
        if layer.get("type") == "logo" or (
            layer.get("type") == "image" and layer.get("role") == "logo"
        ):
            return layer
    return None


def is_protected(layer: dict[str, Any]) -> bool:
    return bool((layer.get("edit_policy") or {}).get("ai_locked"))


def mark_manual_edit(layer: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(layer)
    policy = copy.deepcopy(updated.get("edit_policy") or {})
    policy.update({"ai_locked": True, "manual_edited": True})
    updated["edit_policy"] = policy
    provenance = copy.deepcopy(updated.get("provenance") or {})
    provenance["last_modified_by"] = "user"
    updated["provenance"] = provenance
    return updated


def apply_layer_patch(layer: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(patch, dict):
        raise DesignError("invalid_patch", "修改内容必须是对象")
    updated = copy.deepcopy(layer)
    layer_type = updated.get("type")
    allowed_common = {"visible"}
    if layer_type == "text":
        allowed = allowed_common | {"content", "style", "position", "size", "rotation"}
    elif layer_type in {"image", "logo"}:
        allowed = allowed_common | {
            "position", "size", "opacity", "rotation", "asset_id", "fit", "crop", "mode", "render_hint"
        }
    elif layer_type == "shape":
        allowed = allowed_common | {
            "position", "size", "opacity", "rotation", "fill", "stroke", "radius", "points"
        }
    elif layer_type == "visual_component":
        allowed = allowed_common | {"content", "position", "size", "rotation", "style", "opacity", "compositing"}
    elif layer_type == "background":
        allowed = set()
    else:
        raise DesignError("invalid_layer", "未知图层类型")
    unknown = set(patch) - allowed
    if unknown:
        raise DesignError("field_not_allowed", f"{layer_type} 图层不允许修改字段：{', '.join(sorted(unknown))}")
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
            if key in style_patch and isinstance(style_patch[key], dict) and isinstance(merged.get(key), dict):
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
        try:
            updated["position"] = {
                "x": int(max(-10000, float(position.get("x", updated["position"]["x"])))),
                "y": int(max(-10000, float(position.get("y", updated["position"]["y"])))),
            }
        except (TypeError, ValueError) as exc:
            raise DesignError("invalid_position", "位置必须是数字") from exc
    if "size" in patch:
        size = patch["size"]
        if not isinstance(size, dict):
            raise DesignError("invalid_size", "尺寸必须是对象")
        try:
            width = float(size.get("width", updated.get("size", {}).get("width", 100)))
            height = float(size.get("height", updated.get("size", {}).get("height", 100)))
        except (TypeError, ValueError) as exc:
            raise DesignError("invalid_size", "尺寸必须是数字") from exc
        if width <= 0 or height <= 0:
            raise DesignError("invalid_size", "尺寸必须大于 0")
        updated["size"] = {"width": int(max(16, width)), "height": int(max(16, height))}
    if "visible" in patch:
        updated["visible"] = bool(patch["visible"])
    if "rotation" in patch:
        high = 45 if layer_type == "text" else 180
        try:
            updated["rotation"] = float(_clamp(float(patch["rotation"]), -high, high))
        except (TypeError, ValueError) as exc:
            raise DesignError("invalid_rotation", "旋转角度必须是数字") from exc
    if "opacity" in patch and layer_type in {"image", "logo", "shape"}:
        try:
            updated["opacity"] = float(_clamp(float(patch["opacity"]), 0.05, 1.0))
        except (TypeError, ValueError) as exc:
            raise DesignError("invalid_opacity", "透明度必须是数字") from exc
    if "asset_id" in patch and layer_type in {"image", "logo"}:
        asset_id = patch["asset_id"]
        if not isinstance(asset_id, str) or not asset_id:
            raise DesignError("invalid_asset", "图片素材 id 无效")
        updated["asset_id"] = asset_id
    if "fit" in patch and layer_type in {"image", "logo"}:
        if patch["fit"] not in {"contain", "cover"}:
            raise DesignError("invalid_fit", "fit 只能是 contain 或 cover")
        updated["fit"] = patch["fit"]
    if "crop" in patch and layer_type in {"image", "logo"}:
        updated["crop"] = _normalize_crop(patch["crop"])
    if "mode" in patch and layer_type in {"image", "logo"}:
        if patch["mode"] not in {"model_integrated", "overlay"}:
            raise DesignError("invalid_mode", "Logo 模式无效")
        updated["mode"] = patch["mode"]
        updated["render_hint"] = "baked" if patch["mode"] == "model_integrated" else "overlay"
    if "render_hint" in patch and layer_type in {"image", "logo"}:
        if patch["render_hint"] not in {"baked", "overlay"}:
            raise DesignError("invalid_render_hint", "render_hint 无效")
        updated["render_hint"] = patch["render_hint"]
    if layer_type == "visual_component":
        if "content" in patch:
            raw_content = patch["content"]
            if isinstance(raw_content, str):
                raw_content = {"text": raw_content}
            if not isinstance(raw_content, dict):
                raise DesignError("invalid_content", "组件内容必须是对象或文字")
            merged_content = copy.deepcopy(updated.get("content") or {})
            merged_content.update(copy.deepcopy(raw_content))
            if "text" in merged_content and not isinstance(merged_content["text"], str):
                raise DesignError("invalid_content", "组件文字必须是字符串")
            if isinstance(merged_content.get("text"), str) and len(merged_content["text"]) > 500:
                raise DesignError("content_too_long", "组件文字不能超过 500 字")
            if merged_content.get("component_type") == "price" and isinstance(merged_content.get("text"), str):
                merged_content["amount"] = merged_content["text"]
            updated["content"] = merged_content
            text_children = [child for child in updated.get("children") or [] if isinstance(child, dict) and child.get("type") == "text"]
            incoming_text = merged_content.get("text") if isinstance(merged_content.get("text"), str) else None
            if incoming_text is not None and text_children:
                parts = [part for part in incoming_text.split() if part]
                if len(parts) == len(text_children):
                    for child, part in zip(text_children, parts):
                        child["content"] = part
                        child["visible"] = True
                else:
                    text_children[0]["content"] = incoming_text
                    for child in text_children[1:]:
                        child["visible"] = False
            updated = mark_manual_edit(updated)
        if "style" in patch and isinstance(patch["style"], dict):
            updated["style"] = {**copy.deepcopy(updated.get("style") or {}), **copy.deepcopy(patch["style"])}
        if "compositing" in patch and isinstance(patch["compositing"], dict):
            updated["compositing"] = {**copy.deepcopy(updated.get("compositing") or {}), **copy.deepcopy(patch["compositing"])}
        if "opacity" in patch:
            try:
                updated["opacity"] = float(_clamp(float(patch["opacity"]), 0.05, 1.0))
            except (TypeError, ValueError) as exc:
                raise DesignError("invalid_opacity", "透明度必须是数字") from exc
        if "position" in patch:
            position = patch["position"]
            updated["position"] = {"x": int(position.get("x", updated["position"]["x"])), "y": int(position.get("y", updated["position"]["y"]))}
        if "size" in patch:
            size = patch["size"]
            updated["size"] = {"width": max(8, int(size.get("width", updated["size"]["width"]))), "height": max(8, int(size.get("height", updated["size"]["height"])))}
        if "rotation" in patch:
            updated["rotation"] = float(_clamp(float(patch["rotation"]), -180, 180))
        return updated
    if layer_type == "shape":
        if "fill" in patch:
            updated["fill"] = parse_color(patch["fill"], "#00000000") if patch["fill"] else None
        if "stroke" in patch:
            stroke = patch["stroke"]
            if stroke is None:
                updated["stroke"] = None
            elif isinstance(stroke, dict):
                try:
                    stroke_width = int(_clamp(float(stroke.get("width", 0)), 0, 80))
                except (TypeError, ValueError):
                    stroke_width = 0
                updated["stroke"] = {"width": stroke_width, "color": parse_color(stroke.get("color"), "#000000")} if stroke_width else None
            else:
                raise DesignError("invalid_stroke", "描边必须是对象")
        if "radius" in patch:
            try:
                updated["radius"] = int(_clamp(float(patch["radius"]), 0, 500))
            except (TypeError, ValueError):
                pass
        if "points" in patch and isinstance(patch["points"], list):
            normalized = []
            for item in patch["points"]:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    normalized.append([float(item[0]), float(item[1])])
            updated["points"] = normalized
    return updated


def replace_layer(design: dict[str, Any], updated: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(design)
    for index, layer in enumerate(result.get("layers", [])):
        if layer.get("id") == updated.get("id"):
            result["layers"][index] = copy.deepcopy(updated)
            return result
    raise DesignError("layer_not_found", f"图层不存在：{updated.get('id')}")


def design_summary(design: dict[str, Any]) -> dict[str, Any]:
    layers = []
    for layer in sorted_layers(design):
        item: dict[str, Any] = {
            "id": layer.get("id"),
            "type": layer.get("type"),
            "name": layer.get("name"),
            "visible": layer.get("visible", True),
            "position": layer.get("position"),
            "size": layer.get("size"),
            "rotation": layer.get("rotation", 0),
            "z_index": layer.get("z_index", 0),
            "ai_locked": is_protected(layer),
        }
        if layer.get("type") == "text":
            item["role"] = layer.get("role")
            item["content"] = layer.get("content")
            style = layer.get("style") or {}
            item["style"] = {
                "font_size": style.get("font_size"),
                "font_weight": style.get("font_weight"),
                "font_family": style.get("font_family"),
                "color": style.get("color"),
                "align": style.get("align"),
            }
        if layer.get("type") == "image":
            item["role"] = layer.get("role")
            item["opacity"] = layer.get("opacity")
            item["fit"] = layer.get("fit")
        if layer.get("type") == "shape":
            item["shape"] = layer.get("shape")
            item["fill"] = layer.get("fill")
        if layer.get("type") == "visual_component":
            item["semantic_type"] = layer.get("semantic_type")
            item["render_mode"] = layer.get("render_mode")
            item["content"] = layer.get("content")
            item["compositing"] = layer.get("compositing")
            item["relations"] = layer.get("relations")
        layers.append(item)
    return {
        "canvas": design.get("canvas"),
        "design_intent": design.get("design_intent"),
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


def copy_items_from_design(design: dict[str, Any]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for layer in text_layers(design):
        items.append({
            "id": str(layer.get("copy_id") or f"copy_{layer.get('id')}"),
            "role": str(layer.get("role") or "headline"),
            "text": str(layer.get("content") or ""),
            "source": str((layer.get("provenance") or {}).get("copy_source") or "creative"),
        })
    return items


def scale_box(box: dict[str, Any], scale: float) -> dict[str, int]:
    return {
        "width": max(16, int(float(box.get("width", 100)) * scale)),
        "height": max(16, int(float(box.get("height", 100)) * scale)),
    }
