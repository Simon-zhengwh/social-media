from __future__ import annotations

import base64
import io
from datetime import datetime, timezone

import mongomock
from PIL import Image

from app.services import design as design_utils
from app.services import pipeline, storage, visual_critic


class ZeroProductCoreAI:
    def __init__(self) -> None:
        self.generated = 0
        self.edited = 0

    def generate_image(self, prompt, *, size, model=None, quality=None):
        self.generated += 1
        buffer = io.BytesIO()
        Image.new("RGB", (700, 700), (20, 40, 25)).save(buffer, format="PNG")
        return buffer.getvalue()

    def edit_image(self, prompt, references, *, size, model=None, quality=None, mask=None):
        self.edited += 1
        raise AssertionError("zero-product task must not call edit_image")

    def chat_json(self, messages, *, model=None, timeout_seconds=None):
        return {}


def test_v2_design_migrates_logo_to_image_layer():
    layers = [
        {"id": "background", "type": "background", "name": "底图", "asset_id": "asset_bg", "z_index": 0, "visible": True, "locked": True},
        {"id": "text_headline", "type": "text", "name": "主标题", "role": "headline", "content": "周末相聚", "position": {"x": 80, "y": 120}, "size": {"width": 800, "height": 240}, "style": {"font_size": 120}, "rotation": 14, "z_index": 10, "visible": True, "locked": False},
        {"id": "logo_brand", "type": "logo", "name": "品牌 Logo", "asset_id": "asset_logo", "position": {"x": 800, "y": 50}, "size": {"width": 120, "height": 120}, "opacity": 1.0, "rotation": 0, "mode": "model_integrated", "z_index": 20, "visible": True, "locked": False},
    ]
    design = {"schema_version": 2, "canvas": {"width": 1080, "height": 1350}, "background": {"clean_asset_id": "asset_clean", "render_asset_id": "asset_render", "prompt": "场景", "logo_integrated": True}, "layers": layers}
    upgraded = design_utils.upgrade_design(design)
    assert upgraded["schema_version"] == 3
    assert upgraded["layers"][1]["rotation"] == 14
    logo = design_utils.logo_layer(upgraded)
    assert logo["type"] == "image" and logo["role"] == "logo"
    assert logo["render_hint"] == "baked"


def test_render_image_and_shape_layers():
    from app.services.renderer import render_design
    from app.config import get_settings

    background = Image.new("RGB", (400, 400), (10, 20, 30))
    photo = Image.new("RGBA", (100, 80), (220, 40, 20, 255))
    shape = design_utils.make_shape_layer(layer_id="layer_shape", name="色块", shape="ellipse", box={"x": 20, "y": 20, "width": 120, "height": 80}, fill="#FFE36BCC", z_index=10)
    image = design_utils.make_image_layer(layer_id="layer_photo", name="商品", asset_id="asset_photo", role="product", box={"x": 160, "y": 120, "width": 160, "height": 120}, fit="contain", z_index=20)
    result = render_design(get_settings(), background, [shape, image], (400, 400), assets={"asset_photo": photo})
    assert result.image.size == (400, 400)
    assert any(item["type"] == "shape" for item in result.design["layers"])
    assert any(item["type"] == "image" for item in result.design["layers"])


def test_zero_product_pipeline_uses_generate_image(settings, store):
    db = mongomock.MongoClient().db
    now = datetime.now(timezone.utc)
    task_id = "task_zero"
    db.tasks.insert_one({
        "_id": task_id,
        "status": "queued",
        "stage": "pending",
        "progress": 0,
        "input": {"product_asset_ids": [], "logo_asset_id": None, "brand_id": "seejoy", "prompt": "品牌活动，无价格", "ratio": "1:1"},
        "canvas": {"width": 1024, "height": 1024},
        "provider": {"name": "core-ai", "model": "test", "task_id": None, "status": None, "progress": 0},
        "background_asset_id": None,
        "output_asset_id": None,
        "design": None,
        "current_version": 0,
        "created_at": now,
        "updated_at": now,
    })
    fake = ZeroProductCoreAI()
    pipeline.run_task(db, settings, store, fake, task_id)
    task = db.tasks.find_one({"_id": task_id})
    assert task["status"] == "succeeded"
    assert fake.generated == 1
    assert fake.edited == 0
    assert task["design"]["schema_version"] == 3
    assert not any("data:image" in str(message) for message in task.get("messages") or [])


def test_critic_model_image_is_downscaled_jpeg():
    large = Image.new("RGB", (2400, 2400), (100, 120, 140))
    data_url = visual_critic.model_image_data_url(large)
    assert data_url.startswith("data:image/jpeg;base64,")
    data = base64.b64decode(data_url.split(",", 1)[1])
    assert len(data) <= visual_critic.MAX_MODEL_IMAGE_BYTES
    with Image.open(io.BytesIO(data)) as image:
        assert max(image.size) <= 1024


def test_critic_rejects_copy_changes():
    headline = design_utils.make_text_layer(layer_id="layer_title", name="主标题", role="headline", content="周末相聚", box={"x": 40, "y": 40, "width": 600, "height": 160})
    design = design_utils.make_scene(canvas={"width": 800, "height": 800}, clean_asset_id="asset_bg", render_asset_id="asset_bg", background_prompt="场景", logo_integrated=False, layers=[headline])

    class BadCritic:
        def chat_json(self, messages, *, model=None, timeout_seconds=None):
            return {"issues": [{"severity": "major", "suggested_actions": [{"type": "update_text", "layer_id": "layer_title", "content": "被改写"}]}]}

    settings = type("S", (), {"core_ai_design_model": "test"})()
    review = visual_critic.review_candidate(settings, BadCritic(), candidate_id="c1", image=Image.new("RGB", (800, 800), (20, 20, 20)), design=design, quality_report={"warnings": []})
    assert review["actions"] == []

def test_text_runs_render_multiple_colors():
    from app.services.renderer import render_design
    from app.config import get_settings

    layer = design_utils.make_text_layer(
        layer_id="layer_title",
        name="主标题",
        role="headline",
        content="周末相聚",
        box={"x": 20, "y": 20, "width": 360, "height": 120},
        style={"font_size": 72, "font_family": "brush", "color": "#FFF4DF", "runs": [{"text": "周末", "color": "#0D4317"}, {"text": "相聚", "color": "#B12310"}], "stroke": {"width": 6, "color": "#FFF4DF"}},
    )
    result = render_design(get_settings(), Image.new("RGB", (400, 200), (255, 255, 255)), [layer], (400, 200))
    colors = result.image.convert("RGB").getcolors(maxcolors=200000) or []
    assert any(red > 120 and green < 80 for _, (red, green, blue) in colors)
    assert any(red < 80 and green < 100 for _, (red, green, blue) in colors)

def test_visual_component_renders_children_without_losing_content():
    title_text = design_utils.make_text_layer(
        layer_id="title_a", name="前半", role="headline", content="周末",
        box={"x": 0, "y": 0, "width": 180, "height": 100},
        style={"font_size": 64, "font_family": "brush", "color": "#0D4317"},
    )
    title_text_b = design_utils.make_text_layer(
        layer_id="title_b", name="后半", role="headline", content="相聚",
        box={"x": 170, "y": 0, "width": 180, "height": 100},
        style={"font_size": 64, "font_family": "brush", "color": "#B12310"},
    )
    underline = design_utils.make_shape_layer(
        layer_id="title_underline", name="笔刷下划线", shape="line",
        box={"x": 0, "y": 82, "width": 340, "height": 16},
        fill=None, stroke={"width": 8, "color": "#FFE36B"}, points=[[0, 0.6], [1, 0.4]],
    )
    component = design_utils.make_visual_component(
        component_id="component_title", name="主标题组件", semantic_type="headline",
        content={"text": "周末相聚"}, children=[title_text, title_text_b, underline],
        box={"x": 20, "y": 20, "width": 360, "height": 120}, z_index=500,
    )
    design = design_utils.make_scene(
        canvas={"width": 420, "height": 220}, clean_asset_id="asset_bg", render_asset_id="asset_bg",
        background_prompt="场景", logo_integrated=False, layers=[component],
    )
    design_utils.validate_design(design)
    from app.services.renderer import render_design
    from app.config import get_settings
    result = render_design(get_settings(), Image.new("RGB", (420, 220), (10, 20, 10)), design["layers"], (420, 220))
    metrics = [item for item in result.design["layers"] if item.get("type") == "visual_component"]
    assert metrics and metrics[0]["children"]
    assert component["content"]["text"] == "周末相聚"


def test_duplicate_copy_id_is_collapsed_in_layout():
    copy_items = [{"id": "copy_01", "role": "headline", "text": "周末", "source": "user"}]
    layers = [
        {"id": "a", "copy_id": "copy_01", "role": "headline", "box": {"x": 10, "y": 10, "width": 100, "height": 50}},
        {"id": "b", "copy_id": "copy_01", "role": "headline", "box": {"x": 20, "y": 20, "width": 100, "height": 50}},
    ]
    from app.services.designer import _normalize_layout_layers
    result = _normalize_layout_layers(layers, canvas={"width": 420, "height": 420}, copy_items=copy_items)
    assert len(result) == 1
