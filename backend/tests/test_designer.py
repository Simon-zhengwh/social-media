from __future__ import annotations

from types import SimpleNamespace

from app.config import PROJECT_DIR
from app.services import brand_style
from app.services import designer


def seejoy_brand():
    return brand_style.get_brand(PROJECT_DIR / ".agents" / "skills", "seejoy")


def test_logo_overlap_shrinks_headline_box():
    layout = {
        "layers": [
            {
                "role": "headline",
                "content": "畅吃自助",
                "box": {"x": 185, "y": 135, "width": 760, "height": 185},
            }
        ],
        "logo_box": {"x": 879, "y": 38, "width": 155, "height": 100},
    }
    designer._resolve_logo_overlap(layout, {"width": 1080, "height": 1920})
    headline = layout["layers"][0]["box"]
    logo = layout["logo_box"]
    assert headline["x"] + headline["width"] <= logo["x"]


def test_fallback_layout_reserves_logo_zone():
    canvas = {"width": 1080, "height": 1920}
    copy = {
        "headline": "畅吃自助",
        "subheadline": "火锅 · 寿司 · 甜品",
        "price": "$56.99/位",
        "details": "12PM - 12AM",
    }
    layout = designer.fallback_layout(canvas, copy, has_logo=True)
    designer._resolve_logo_overlap(layout, canvas)
    logo = layout["logo_box"]
    for item in layout["layers"]:
        box = item["box"]
        overlaps = (
            box["x"] < logo["x"] + logo["width"]
            and box["x"] + box["width"] > logo["x"]
            and box["y"] < logo["y"] + logo["height"]
            and box["y"] + box["height"] > logo["y"]
        )
        assert not overlaps, item["role"]


def test_logo_box_preserves_original_aspect_ratio():
    layout = {
        "layers": [],
        "logo_box": {"x": 879, "y": 38, "width": 155, "height": 100},
    }
    designer.fit_logo_box(
        layout, {"width": 1080, "height": 1920}, logo_aspect=1.0
    )
    assert layout["logo_box"]["width"] == layout["logo_box"]["height"]


def test_top_left_clump_is_reflowed_into_ad_layout():
    canvas = {"width": 1080, "height": 1350}
    copy = {
        "brand": "SEEJOY 喜聚",
        "headline": "夜宵时间到",
        "subheadline": "火锅 · 寿司 · 甜品",
        "offer": "儿童5折｜5岁以下免费",
        "price": "$56.99/位",
        "cta": "欢迎到店",
        "details": "12PM - 12AM 119 W 31st St",
    }
    clumped = {
        "layers": [
            {
                "role": "headline",
                "content": copy["headline"],
                "box": {"x": 30, "y": 130, "width": 530, "height": 100},
            },
            {
                "role": "price",
                "content": copy["price"],
                "box": {"x": 33, "y": 376, "width": 315, "height": 62},
            },
        ],
        "logo_box": None,
    }
    layout = designer.repair_layout(
        clumped, canvas, copy, has_logo=True
    )
    assert layout["source"] == "promo-reflow"
    boxes = {item["role"]: item["box"] for item in layout["layers"]}
    assert boxes["price"]["y"] > canvas["height"] * 0.55
    assert boxes["headline"]["height"] >= canvas["height"] * 0.15
    assert any(item.get("rotation") for item in layout["layers"])
    offer = next(item for item in layout["layers"] if item["role"] == "offer")
    assert offer["background"]["color"].startswith("#")


def test_layout_to_layers_keeps_exact_copy():
    canvas = {"width": 1080, "height": 1920}
    copy = {"headline": "畅吃自助", "price": "$56.99/位"}
    layout = designer.fallback_layout(canvas, copy, has_logo=False)
    layers, logo = designer.layout_to_layers(
        layout, canvas=canvas, logo_asset_id=None
    )
    contents = {layer["role"]: layer["content"] for layer in layers}
    assert contents["headline"] == "畅吃自助"
    assert contents["price"] == "$56.99/位"
    assert logo is None


def test_background_prompt_explicitly_excludes_all_text_and_logo():
    prompt = designer.build_background_prompt(
        "畅吃自助，$56.99/位", has_logo=True, product_count=1
    )
    assert "不要出现任何文字" in prompt
    assert "Logo" in prompt
    assert "二维码" in prompt


def test_fallback_creative_plan_never_puts_copy_in_background_prompt():
    settings = type("S", (), {"design_model_enabled": False})()
    plan = designer.make_creative_plan(
        settings,
        None,
        brief="做一张火锅促销海报",
        product_paths=[],
        has_logo=False,
    )
    assert plan["copy"]["headline"]
    prompt = designer.build_background_prompt(
        plan["background_prompt"], has_logo=False, product_count=0
    )
    assert "不要出现任何文字" in prompt


def test_brand_guidance_is_composed_into_background_prompt():
    prompt = designer.build_background_prompt(
        "夜晚门店，热气腾腾的牛肉火锅",
        has_logo=True,
        product_count=2,
        brand=seejoy_brand(),
    )
    assert "真实餐饮摄影质感" in prompt
    assert "不要出现任何文字" in prompt
    assert "夜晚门店" in prompt
    assert "右上角" in prompt


def test_fallback_creative_plan_uses_brand_layout_guide():
    settings = type("S", (), {"design_model_enabled": False})()
    plan = designer.make_creative_plan(
        settings,
        None,
        brief="SeeJoy 周末自助",
        product_paths=[],
        has_logo=False,
        brand=seejoy_brand(),
    )
    assert plan["creative_direction"] == seejoy_brand().layout_guide


def test_promo_layout_uses_brand_palette_and_display_font():
    canvas = {"width": 1080, "height": 1920}
    copy = {"headline": "畅吃自助", "price": "$56.99/位", "offer": "儿童5折"}
    layout = designer.promo_layout(
        canvas, copy, has_logo=True, brand=seejoy_brand()
    )
    layers = {item["role"]: item for item in layout["layers"]}
    assert layers["headline"]["font_family"] == "brush"
    assert layers["price"]["font_family"] == "sans"
    assert layers["price"]["background"]["color"] == "#B12310FF"
    text_layers, _ = designer.layout_to_layers(
        layout, canvas=canvas, logo_asset_id=None
    )
    styles = {layer["role"]: layer["style"] for layer in text_layers}
    assert styles["headline"]["font_family"] == "brush"
    assert styles["price"]["font_family"] == "sans"


def test_creative_plan_sends_three_style_references_to_design_model():
    class CaptureClient:
        def __init__(self):
            self.messages = None

        def chat_json(self, messages, model=None):
            self.messages = messages
            return {
                "creative_direction": "品牌拼贴",
                "background_prompt": "深绿色墙面前摆满新鲜牛肉和蔬菜的火锅餐桌",
                "copy": {"headline": "畅吃自助"},
            }

    client = CaptureClient()
    settings = SimpleNamespace(
        design_model_enabled=True,
        toapis_design_model="test-design-model",
    )
    plan = designer.make_creative_plan(
        settings,
        client,
        brief="SeeJoy 夜宵自助",
        product_paths=[],
        has_logo=False,
        brand=seejoy_brand(),
    )
    assert plan["source"] == "model"
    user_content = client.messages[-1]["content"]
    image_count = sum(1 for item in user_content if item["type"] == "image_url")
    assert image_count == 3
    prompt_text = next(item["text"] for item in user_content if item["type"] == "text")
    assert "SeeJoy" in prompt_text
    style_text = [
        item["text"] for item in user_content if item["type"] == "text"
    ][1]
    assert "只学习配色" in style_text
