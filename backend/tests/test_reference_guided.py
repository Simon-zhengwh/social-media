from __future__ import annotations

import io
from datetime import datetime, timezone

import mongomock
from PIL import Image, ImageDraw

from app.services import design as design_utils
from app.services import pipeline, reference_guided, storage


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _seed_asset(db, settings, store, data: bytes, name: str, kind: str) -> dict:
    prepared = storage.prepare_upload(settings, store, data, name, kind)
    doc = {
        "_id": prepared.id,
        "kind": kind,
        "original_name": prepared.original_name,
        "path": prepared.path,
        "prepared_path": prepared.prepared_path,
        "mime_type": prepared.mime_type,
        "width": prepared.width,
        "height": prepared.height,
        "bytes": prepared.bytes,
        "sha256": prepared.sha256,
        "created_at": datetime.now(timezone.utc),
    }
    db.assets.insert_one(doc)
    return doc


class ReferenceCoreAI:
    def __init__(self) -> None:
        self.image_calls: list[str] = []
        self.reference_counts: list[int] = []

    def edit_image(self, prompt, references, *, size, model=None, quality=None, mask=None):
        self.image_calls.append(prompt)
        self.reference_counts.append(len(references))
        if "finished commercial social-media poster" in prompt:
            return _png(Image.new("RGB", (900, 1600), (28, 51, 32)))
        if "clean photographic plate" in prompt:
            return _png(Image.new("RGB", (900, 1600), (43, 66, 38)))
        image = Image.new("RGB", (1024, 1024), (255, 0, 255))
        draw = ImageDraw.Draw(image)
        if "price ribbon" in prompt:
            draw.polygon([(90, 390), (900, 330), (980, 650), (120, 710)], fill=(177, 35, 16))
            draw.line([(140, 500), (900, 450)], fill=(255, 227, 107), width=18)
        elif "offer sticker" in prompt:
            draw.ellipse((250, 260, 790, 760), fill=(217, 45, 32))
            draw.ellipse((310, 320, 730, 700), outline=(255, 244, 223), width=20)
        else:
            draw.polygon([(90, 410), (480, 220), (550, 740), (90, 690)], fill=(13, 67, 23))
            draw.polygon([(500, 260), (950, 330), (930, 740), (540, 700)], fill=(177, 35, 16))
            draw.line([(130, 650), (880, 560)], fill=(255, 227, 107), width=24)
        return _png(image)

    def generate_image(self, prompt, *, size, model=None, quality=None):
        self.image_calls.append(prompt)
        return _png(Image.new("RGB", (900, 1600), (28, 51, 32)))

    def chat_json(self, messages, *, model=None, timeout_seconds=None):
        return {}


def _task(db, product):
    now = datetime.now(timezone.utc)
    task_id = "task_reference_guided"
    plan = {
        "design_intent": design_utils.normalize_design_intent({
            "concept": "SeeJoy 周末火锅促销，深绿与品牌红，毛笔标题和手绘价格丝带",
            "composition": "满版餐桌摄影，标题压在左上，价格丝带落在右下",
            "typography": "超大毛笔标题，价格用粗黑体",
            "text_image_relation": "标题与笔刷、价格与丝带分别形成整体组件",
        }),
        "background_prompt": "真实火锅餐桌，深绿与暖木色，留出标题和价格空间",
        "copy": {"headline": "周末相聚", "price": "$56.99/位", "offer": "儿童5折", "cta": "约朋友来吃"},
        "copy_items": [
            {"id": "copy_headline", "role": "headline", "text": "周末相聚", "source": "user"},
            {"id": "copy_price", "role": "price", "text": "$56.99/位", "source": "user"},
            {"id": "copy_offer", "role": "offer", "text": "儿童5折", "source": "user"},
            {"id": "copy_cta", "role": "cta", "text": "约朋友来吃", "source": "creative"},
        ],
        "asset_requests": [{"id": "asset_product", "role": "product", "asset_id": product["_id"], "purpose": "底图主体"}],
        "initial_scene_plan": {"layers": [
            {"id": "layer_headline", "type": "text", "copy_id": "copy_headline", "role": "headline", "box": {"x": 52, "y": 230, "width": 900, "height": 240}, "font_size": 178, "font_family": "brush", "font_weight": 900, "color": "#FFF4DF", "rotation": -2, "z_index": 500},
            {"id": "layer_price", "type": "text", "copy_id": "copy_price", "role": "price", "box": {"x": 360, "y": 1450, "width": 620, "height": 160}, "font_size": 104, "font_weight": 900, "color": "#FFF4DF", "z_index": 600},
            {"id": "layer_offer", "type": "text", "copy_id": "copy_offer", "role": "offer", "box": {"x": 72, "y": 540, "width": 330, "height": 142}, "font_size": 78, "font_weight": 900, "color": "#FFE36B", "rotation": -4, "z_index": 560},
            {"id": "layer_cta", "type": "text", "copy_id": "copy_cta", "role": "cta", "box": {"x": 80, "y": 1690, "width": 500, "height": 90}, "font_size": 50, "font_weight": 700, "color": "#FFF4DF", "z_index": 520},
            {"id": "shape_photo_paper", "type": "shape", "name": "照片卡片边框", "shape": "rect", "box": {"x": 24, "y": 526, "width": 1036, "height": 1194}, "fill": "#FFF4DF", "z_index": 10},
            {"id": "layer_product_overlay", "type": "image", "name": "原始商品图", "asset_id": product["_id"], "role": "product", "box": {"x": 40, "y": 550, "width": 1000, "height": 1140}, "z_index": 300},
        ]},
    }
    db.tasks.insert_one({
        "_id": task_id,
        "status": "queued",
        "stage": "pending",
        "progress": 0,
        "input": {
            "product_asset_ids": [product["_id"]],
            "logo_asset_id": None,
            "brand_id": "seejoy",
            "prompt": "SeeJoy 周末火锅促销，原文：周末相聚；价格：$56.99/位；优惠：儿童5折",
            "ratio": "9:16",
            "generation_mode": "reference_guided",
        },
        "brand": {
            "id": "seejoy",
            "name": "SEEJOY 喜聚",
            "short_name": "喜聚",
            "summary": "测试品牌",
            "layout_guide": "SeeJoy 测试排版指南",
            "background_guide": "SeeJoy 测试背景指南",
            "logo_keywords": [],
        },
        "canvas": {"width": 1080, "height": 1920},
        "provider": {"name": "core-ai", "model": "test", "task_id": None, "status": None, "progress": 0},
        "creative_plan": plan,
        "background_asset_id": None,
        "background_clean_asset_id": None,
        "output_asset_id": None,
        "design": None,
        "current_version": 0,
        "messages": [],
        "edit": {"status": "idle", "instruction": None, "error": None},
        "created_at": now,
        "updated_at": now,
    })
    return task_id


def _settings(settings):
    return settings.model_copy(update={
        "reference_guided_enabled": True,
        "component_generation_enabled": True,
        "component_generation_cache_enabled": True,
        "visual_critic_enabled": False,
    })


def test_magenta_key_extracts_transparent_brush():
    image = Image.new("RGB", (640, 400), (255, 0, 255))
    draw = ImageDraw.Draw(image)
    draw.polygon([(80, 170), (300, 70), (340, 310), (90, 300)], fill=(20, 82, 38))
    draw.polygon([(330, 90), (590, 130), (560, 330), (350, 300)], fill=(180, 30, 12))
    art, coverage = reference_guided.extract_transparent_artwork(_png(image))
    assert art.mode == "RGBA"
    assert 0.1 < coverage < 0.8
    assert art.getpixel((0, 0))[3] == 0
    assert art.getchannel("A").getextrema()[1] == 255


def test_price_prompt_forbids_literal_price_text():
    from app.services import brand_style
    from app.config import PROJECT_DIR
    brand = brand_style.load_brand_skill(PROJECT_DIR / ".agents" / "skills" / "seejoy-brand-poster")
    prompt = reference_guided.component_prompt(
        kind="price", brand=brand, intent={}, text_hint="$56.99/位", context_count=2,
    )
    assert "$56.99" not in prompt
    assert "Native price typography will be added later" in prompt
    assert "Do not copy any typography" in prompt


def test_baked_light_glyph_detector_separates_text_from_flecks():
    image = Image.new("RGBA", (600, 240), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((30, 30, 570, 210), fill=(180, 20, 10, 255))
    draw.rectangle((190, 70, 230, 180), fill=(255, 244, 223, 255))
    draw.rectangle((270, 70, 310, 180), fill=(255, 244, 223, 255))
    assert reference_guided.baked_light_glyph_ratio(image) > 0.05
    flecks = Image.new("RGBA", (600, 240), (0, 0, 0, 0))
    draw = ImageDraw.Draw(flecks)
    draw.rectangle((30, 30, 570, 210), fill=(180, 20, 10, 255))
    for x in range(50, 550, 25):
        draw.ellipse((x, 70, x + 4, 74), fill=(255, 244, 223, 255))
    assert reference_guided.baked_light_glyph_ratio(flecks) < 0.05


def test_reference_guided_task_builds_three_visual_components(settings, store, product_png):
    db = mongomock.MongoClient().db
    live_settings = _settings(settings)
    product = _seed_asset(db, live_settings, store, product_png, "hotpot.png", "product")
    task_id = _task(db, product)
    fake = ReferenceCoreAI()
    pipeline.run_task(db, live_settings, store, fake, task_id)
    task = db.tasks.find_one({"_id": task_id})
    assert task["status"] == "succeeded", task.get("error")
    assert task["design"]["generation_mode"] == "reference_guided"
    components = [layer for layer in task["design"]["layers"] if layer["type"] == "visual_component"]
    assert {layer["semantic_type"] for layer in components} == {"headline", "promotion_price", "offer_badge"}
    for component in components:
        art = [child for child in component["children"] if (child.get("provenance") or {}).get("component_art")]
        assert len(art) == 1
        assert art[0]["asset_id"]
        assert db.assets.find_one({"_id": art[0]["asset_id"]})["kind"] == "component"
    assert len(fake.image_calls) == 5
    component_prompts = [prompt for prompt in fake.image_calls if "Create ONLY one isolated poster visual component" in prompt]
    assert len(component_prompts) == 3
    assert all("CURRENT POSTER COMPOSITE" in prompt for prompt in component_prompts)
    assert all("Target region in the current poster" in prompt for prompt in component_prompts)
    context_counts = [
        count for prompt, count in zip(fake.image_calls, fake.reference_counts)
        if "Create ONLY one isolated poster visual component" in prompt
    ]
    assert context_counts == [3, 3, 3]
    assert not any(layer.get("role") == "product" for layer in task["design"]["layers"] if layer.get("type") == "image")
    assert not any(layer.get("id") == "shape_photo_paper" for layer in task["design"]["layers"])
    for component in components:
        assert component["compositing"]["blend_mode"] == "normal"
        assert component["relations"]["scene_layer_id"] == "background"
    assert task["design_reference_asset_id"]
    assert task["output_asset_id"]


def test_component_regeneration_keeps_text_and_changes_only_art(settings, store, product_png):
    db = mongomock.MongoClient().db
    live_settings = _settings(settings)
    product = _seed_asset(db, live_settings, store, product_png, "hotpot.png", "product")
    task_id = _task(db, product)
    fake = ReferenceCoreAI()
    pipeline.run_task(db, live_settings, store, fake, task_id)
    before = db.tasks.find_one({"_id": task_id})
    component = next(layer for layer in before["design"]["layers"] if layer.get("semantic_type") == "headline")
    old_art = next(child for child in component["children"] if (child.get("provenance") or {}).get("component_art"))
    old_text = [child["content"] for child in component["children"] if child.get("type") == "text"]
    pipeline.run_component_regeneration(db, live_settings, store, fake, task_id, component["id"])
    after = db.tasks.find_one({"_id": task_id})
    updated = design_utils.find_layer(after["design"], component["id"])
    new_art = next(child for child in updated["children"] if (child.get("provenance") or {}).get("component_art"))
    assert new_art["asset_id"] != old_art["asset_id"]
    assert [child["content"] for child in updated["children"] if child.get("type") == "text"] == old_text
    assert after["current_version"] == before["current_version"] + 1
