from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image

from app.config import PROJECT_DIR, get_settings
from app.db import create_client, get_database
from app.services import brand_style
from app.services import design as design_utils
from app.services import pipeline, storage
from app.services.object_store import create_object_store
from app.services.renderer import render_design


def _text_child(layer_id: str, text: str, box: dict, *, size: int, color: str, stroke: int = 0, stroke_color: str = "#FFF4DF", weight: int = 900, family: str = "brush") -> dict:
    return design_utils.make_text_layer(
        layer_id=layer_id,
        name=layer_id,
        role="headline",
        content=text,
        box=box,
        style={
            "font_family": family,
            "font_size": size,
            "font_weight": weight,
            "color": color,
            "stroke": {"width": stroke, "color": stroke_color} if stroke else None,
            "shadow": {"blur": 12, "offset_x": 0, "offset_y": 8, "color": "#00000088"},
        },
        z_index=10,
    )


def _title_component(text: str, box: dict, palette: dict) -> dict:
    midpoint = max(1, len(text) // 2)
    first, second = text[:midpoint], text[midpoint:]
    width = int(box["width"])
    half = int(width * 0.5)
    children = [
        _text_child("title_part_a", first, {"x": 0, "y": 10, "width": half, "height": int(box["height"] * 0.86)}, size=int(box["height"] * 0.72), color=palette.get("primary_deep", "#0D4317"), stroke=max(6, int(width * 0.009)), stroke_color=palette.get("cream", "#FFF4DF")),
        _text_child("title_part_b", second, {"x": half - 20, "y": 0, "width": half + 20, "height": int(box["height"] * 0.86)}, size=int(box["height"] * 0.72), color=palette.get("accent", "#B12310"), stroke=max(6, int(width * 0.009)), stroke_color=palette.get("cream", "#FFF4DF")),
        design_utils.make_shape_layer(
            layer_id="title_brush_underline",
            name="标题笔刷下划线",
            shape="line",
            box={"x": 0, "y": int(box["height"] * 0.84), "width": width, "height": 28},
            fill=None,
            stroke={"width": max(10, int(width * 0.014)), "color": palette.get("highlight", "#FFE36B")},
            opacity=0.9,
            rotation=-1.5,
            z_index=20,
            points=[[0.02, 0.65], [0.48, 0.38], [0.97, 0.55]],
        ),
        design_utils.make_shape_layer(
            layer_id="title_accent_dot",
            name="标题装饰点",
            shape="ellipse",
            box={"x": int(width * 0.01), "y": int(box["height"] * 0.10), "width": 18, "height": 18},
            fill=palette.get("accent_bright", "#D92D20"),
            z_index=25,
        ),
    ]
    return design_utils.make_visual_component(
        component_id="component_title_01",
        name="主标题视觉组件",
        semantic_type="headline",
        content={"text": text, "runs": [{"text": first, "color": "primary"}, {"text": second, "color": "accent"}]},
        children=children,
        box=box,
        render_mode="native",
        z_index=700,
        rotation=-1.2,
    )


def _price_component(box: dict, palette: dict) -> dict:
    width, height = int(box["width"]), int(box["height"])
    children = [
        design_utils.make_shape_layer(
            layer_id="price_brush_ribbon",
            name="价格笔刷丝带",
            shape="polygon",
            box={"x": 0, "y": 0, "width": width, "height": height},
            fill=palette.get("accent", "#B12310"),
            stroke={"width": 4, "color": palette.get("accent_deep", "#7A160F")},
            rotation=-1.2,
            z_index=1,
            points=[[0.01, 0.14], [0.94, 0.02], [0.99, 0.80], [0.06, 0.98], [0.00, 0.62]],
        ),
        _text_child("price_currency", "$", {"x": 18, "y": 55, "width": 64, "height": 82}, size=62, color=palette.get("highlight", "#FFE36B"), weight=900, family="sans"),
        _text_child("price_number", "56.99", {"x": 70, "y": 12, "width": 330, "height": 152}, size=132, color=palette.get("cream", "#FFF4DF"), weight=900, family="sans"),
        _text_child("price_unit", "/位", {"x": 390, "y": 78, "width": 132, "height": 82}, size=54, color=palette.get("highlight", "#FFE36B"), weight=700, family="sans"),
        design_utils.make_shape_layer(
            layer_id="price_accent_line",
            name="价格装饰线",
            shape="line",
            box={"x": int(width * 0.72), "y": 20, "width": int(width * 0.24), "height": 34},
            fill=None,
            stroke={"width": 7, "color": palette.get("highlight", "#FFE36B")},
            z_index=15,
            points=[[0.0, 0.7], [0.4, 0.2], [0.8, 0.8], [1.0, 0.4]],
        ),
    ]
    return design_utils.make_visual_component(
        component_id="component_price_01",
        name="价格促销视觉组件",
        semantic_type="promotion_price",
        content={"currency": "$", "amount": "56.99", "unit": "/位"},
        children=children,
        box=box,
        render_mode="native",
        z_index=720,
        rotation=-1.5,
    )


def _load_assets(db, store, design):
    assets = {}
    for layer in design.get("layers", []):
        if layer.get("type") in {"image", "logo"}:
            asset = db.assets.find_one({"_id": layer.get("asset_id")})
            if asset:
                assets[asset["_id"]] = storage.open_image(store, asset.get("prepared_path") or asset["path"])
        for child in layer.get("children") or []:
            if isinstance(child, dict) and child.get("type") in {"image", "logo"}:
                asset = db.assets.find_one({"_id": child.get("asset_id")})
                if asset:
                    assets[asset["_id"]] = storage.open_image(store, asset.get("prepared_path") or asset["path"])
    return assets


def _save(im: Image.Image, path: Path, max_side: int = 420) -> None:
    im.save(path)
    thumb = im.copy().convert("RGB")
    thumb.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    thumb.save(path.with_name(path.stem + "-thumb.jpg"), quality=86)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default="task_1bd2d1b1240a444b933f1fb10702f8a4")
    parser.add_argument("--out", default=str(PROJECT_DIR / "storage" / "visual-component-experiment"))
    args = parser.parse_args()
    settings = get_settings()
    client = create_client(settings.mongodb_uri)
    db = get_database(client, settings.mongodb_database)
    store = create_object_store(settings)
    task = db.tasks.find_one({"_id": args.task})
    if task is None:
        raise SystemExit("task not found")
    design = design_utils.upgrade_design(task["design"])
    canvas = design["canvas"]
    background = storage.open_image(store, pipeline._background_key(db, design["background"]["render_asset_id"]))
    assets = _load_assets(db, store, design)
    palette = brand_style.DEFAULT_PALETTE

    base_layers = []
    for layer in design["layers"]:
        if layer.get("type") == "text" and layer.get("role") in {"headline", "price"}:
            continue
        base_layers.append(layer)
    before = render_design(settings, background, design_utils.sorted_layers(design), (canvas["width"], canvas["height"]), assets=assets).image
    title = _title_component("周末相聚", {"x": 58, "y": 305, "width": 960, "height": 235}, palette)
    price = _price_component({"x": 480, "y": 1645, "width": 560, "height": 210}, palette)
    after_layers = base_layers + [title, price]
    after = render_design(settings, background, design_utils.sorted_layers({"layers": after_layers}), (canvas["width"], canvas["height"]), assets=assets).image

    edited_title = _title_component("周末限时特惠", {"x": 58, "y": 305, "width": 960, "height": 235}, palette)
    edited_layers = [layer for layer in after_layers if layer.get("id") != title["id"]] + [edited_title]
    edited = render_design(settings, background, design_utils.sorted_layers({"layers": edited_layers}), (canvas["width"], canvas["height"]), assets=assets).image

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    _save(before, out / "before.png")
    _save(after, out / "after.png")
    _save(edited, out / "after-title-edit.png")
    gap = 24
    before_thumb = before.copy(); before_thumb.thumbnail((420, 720))
    after_thumb = after.copy(); after_thumb.thumbnail((420, 720))
    compare = Image.new("RGB", (before_thumb.width + after_thumb.width + gap, max(before_thumb.height, after_thumb.height)), (240, 240, 240))
    compare.paste(before_thumb, (0, 0)); compare.paste(after_thumb, (before_thumb.width + gap, 0))
    _save(compare, out / "compare.png", max_side=900)
    (out / "report.json").write_text(json.dumps({"task": args.task, "title_component": title, "price_component": price, "before": "before.png", "after": "after.png", "after_title_edit": "after-title-edit.png"}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
