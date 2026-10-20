from __future__ import annotations

import copy
import logging
from typing import Any

from app.services import design as design_utils

logger = logging.getLogger(__name__)


def _shape_geometry(layer: dict[str, Any]) -> tuple[dict[str, int], list[list[float]]]:
    raw_points = [[float(p[0]), float(p[1])] for p in (layer.get("points") or []) if isinstance(p, (list, tuple)) and len(p) >= 2]
    box = layer.get("box") if isinstance(layer.get("box"), dict) else None
    if box is None and raw_points:
        min_x = min(p[0] for p in raw_points)
        max_x = max(p[0] for p in raw_points)
        min_y = min(p[1] for p in raw_points)
        max_y = max(p[1] for p in raw_points)
        width = max(8, int(max_x - min_x))
        height = max(8, int(max_y - min_y))
        box = {"x": int(min_x), "y": int(min_y), "width": width, "height": height}
        points = [[(p[0] - min_x) / width, (p[1] - min_y) / height] for p in raw_points]
    elif box is None:
        box = {"x": 0, "y": 0, "width": 200, "height": 40}
        points = raw_points
    else:
        width = max(8, int(float(box.get("width") or 8)))
        height = max(8, int(float(box.get("height") or 8)))
        if raw_points and any(abs(p[0]) > 3 or abs(p[1]) > 3 for p in raw_points):
            points = [[(p[0] - float(box.get("x", 0))) / width, (p[1] - float(box.get("y", 0))) / height] for p in raw_points]
        else:
            points = raw_points
    return box, points


def _box_from_placement(placement: Any, canvas: dict[str, int], *, role: str) -> dict[str, int]:
    if isinstance(placement, dict):
        try:
            return {
                "x": int(placement.get("x", 0)),
                "y": int(placement.get("y", 0)),
                "width": max(16, int(placement.get("width", canvas["width"] * 0.3))),
                "height": max(16, int(placement.get("height", canvas["height"] * 0.3))),
            }
        except (TypeError, ValueError):
            pass
    width = int(canvas["width"])
    height = int(canvas["height"])
    if role == "logo":
        size = int(width * 0.12)
        return {"x": width - int(width * 0.06) - size, "y": int(height * 0.04), "width": size, "height": size}
    if role == "product":
        return {"x": int(width * 0.48), "y": int(height * 0.30), "width": int(width * 0.46), "height": int(height * 0.46)}
    return {"x": int(width * 0.08), "y": int(height * 0.08), "width": int(width * 0.18), "height": int(height * 0.18)}


def build_scene_assets(
    *,
    plan: dict[str, Any],
    canvas: dict[str, int],
    products: list[dict[str, Any]],
    logo: dict[str, Any] | None,
    db: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Reuse approved image assets and deterministic shapes. No paid generation here."""
    image_layers: list[dict[str, Any]] = []
    shape_layers: list[dict[str, Any]] = []
    usage: list[dict[str, Any]] = []
    intent = plan.get("design_intent") or {}
    requests = plan.get("asset_requests") or []
    independent_product_ids = {
        str(item.get("asset_id"))
        for item in requests
        if isinstance(item, dict) and item.get("role") == "product" and item.get("asset_id")
    }
    initial_layers = (plan.get("initial_scene_plan") or {}).get("layers") if isinstance(plan.get("initial_scene_plan"), dict) else []
    initial_layers = initial_layers if isinstance(initial_layers, list) else []

    for layer in initial_layers:
        if not isinstance(layer, dict):
            continue
        if layer.get("type") == "image" and layer.get("asset_id"):
            asset_id = str(layer.get("asset_id"))
            role = str(layer.get("role") or "decoration")
            if logo is not None and asset_id == str(logo["_id"]):
                continue
            if any(asset_id == str(item["_id"]) for item in products):
                continue
            if not db.assets.find_one({"_id": asset_id}):
                continue
            image_layers.append(design_utils.make_image_layer(
                layer_id=str(layer.get("id") or f"layer_image_{len(image_layers) + 1}"),
                name=str(layer.get("name") or "独立图片"),
                asset_id=asset_id,
                role=role,
                box=layer.get("box") or {},
                z_index=int(layer.get("z_index") or 40),
                opacity=float(layer.get("opacity") or 1.0),
                rotation=float(layer.get("rotation") or 0.0),
                fit=str(layer.get("fit") or "contain"),
                crop=layer.get("crop"),
                render_hint="overlay",
            ))
        if layer.get("type") == "shape":
            box, points = _shape_geometry(layer)
            stroke = layer.get("stroke")
            if isinstance(stroke, str):
                stroke = {"width": int(layer.get("stroke_width") or 2), "color": stroke}
            shape_layers.append(design_utils.make_shape_layer(
                layer_id=str(layer.get("id") or f"layer_shape_{len(shape_layers) + 1}"),
                name=str(layer.get("name") or "装饰形状"),
                shape=str(layer.get("shape") or "rect"),
                box=box,
                fill=layer.get("fill"),
                stroke=stroke,
                opacity=float(layer.get("opacity") or 1.0),
                rotation=float(layer.get("rotation") or 0.0),
                z_index=int(layer.get("z_index") or 20),
                points=points,
                radius=int(layer.get("radius") or layer.get("corner_radius") or 0),
            ))

    if logo is not None:
        request = next((item for item in requests if isinstance(item, dict) and item.get("role") == "logo"), {})
        box = _box_from_placement(request.get("placement") if isinstance(request, dict) else None, canvas, role="logo")
        image_layers.append(design_utils.make_image_layer(
            layer_id="layer_logo_01",
            name="品牌 Logo",
            asset_id=str(logo["_id"]),
            role="logo",
            box=box,
            z_index=900,
            fit="contain",
            render_hint="overlay",
        ))
        usage.append({"asset_id": logo["_id"], "role": "logo", "sha256": logo.get("sha256"), "intent_id": intent.get("id"), "intent_hash": intent.get("hash")})

    for product in products:
        if str(product["_id"]) not in independent_product_ids:
            continue
        request = next((item for item in requests if isinstance(item, dict) and str(item.get("asset_id") or "") == str(product["_id"])), {})
        image_layers.append(design_utils.make_image_layer(
            layer_id=f"layer_product_{product['_id'][-8:]}",
            name=str(product.get("original_name") or "独立商品"),
            asset_id=str(product["_id"]),
            role="product",
            box=_box_from_placement(request.get("placement") if isinstance(request, dict) else None, canvas, role="product"),
            z_index=300,
            fit="contain",
            render_hint="overlay",
        ))
        usage.append({"asset_id": product["_id"], "role": "product", "sha256": product.get("sha256"), "intent_id": intent.get("id"), "intent_hash": intent.get("hash")})

    # Add available decoration assets when the plan explicitly asks to reuse them.
    for request in requests:
        if not isinstance(request, dict) or request.get("role") != "decoration":
            continue
        asset_id = str(request.get("asset_id") or "")
        if not asset_id or any(item.get("asset_id") == asset_id for item in image_layers):
            continue
        asset = db.assets.find_one({"_id": asset_id, "kind": "decoration"})
        if asset is None:
            continue
        image_layers.append(design_utils.make_image_layer(
            layer_id=f"layer_decoration_{len(image_layers) + 1}",
            name=str(asset.get("original_name") or "装饰素材"),
            asset_id=asset_id,
            role="decoration",
            box=_box_from_placement(request.get("placement"), canvas, role="decoration"),
            z_index=180,
            fit="contain",
            render_hint="overlay",
        ))
        usage.append({"asset_id": asset_id, "role": "decoration", "sha256": asset.get("sha256"), "intent_id": intent.get("id"), "intent_hash": intent.get("hash")})
    return image_layers, shape_layers, usage
