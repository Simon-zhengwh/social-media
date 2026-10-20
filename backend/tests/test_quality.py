from __future__ import annotations

from PIL import Image

from app.services import quality


def text_layer(color: str = "#FFFFFF") -> dict:
    return {
        "id": "text_headline",
        "type": "text",
        "name": "主标题",
        "content": "限时特惠",
        "visible": True,
        "position": {"x": 10, "y": 10},
        "size": {"width": 180, "height": 80},
        "style": {
            "color": color,
            "stroke": {"width": 0, "color": "#000000"},
            "background": None,
        },
    }


def test_low_contrast_text_is_reported():
    background = Image.new("RGB", (200, 100), (250, 250, 250))
    report = quality.assess_legibility(background, [text_layer("#FFFFFF")])
    assert any(item["code"] == "low_contrast" for item in report["warnings"])


def test_good_contrast_text_is_clean():
    background = Image.new("RGB", (200, 100), (15, 15, 15))
    report = quality.assess_legibility(background, [text_layer("#FFFFFF")])
    assert report["warnings"] == []


def test_either_fill_or_stroke_can_provide_contrast():
    background = Image.new("RGB", (200, 100), (221, 221, 221))
    layer = text_layer("#FFFFFF")
    layer["style"]["stroke"] = {"width": 4, "color": "#101010"}
    report = quality.assess_legibility(background, [layer])
    assert report["warnings"] == []


def test_overflow_metric_is_reported():
    background = Image.new("RGB", (200, 100), (15, 15, 15))
    report = quality.assess_legibility(
        background,
        [text_layer("#FFFFFF")],
        [
            {
                "type": "text",
                "id": "text_headline",
                "overflow": True,
            }
        ],
    )
    assert any(item["code"] == "text_overflow" for item in report["warnings"])


def test_platform_safe_area_and_contact_rules():
    platform = {
        "id": "xiaohongshu",
        "name": "小红书",
        "contact_policy": "forbidden",
        "default_placement_id": "note",
        "placements": [
            {
                "id": "note",
                "name": "图文笔记 / 封面",
                "ratio": "3:4",
                "width": 1080,
                "height": 1440,
                "safe_area": {"top": 0.05, "right": 0.05, "bottom": 0.05, "left": 0.05},
            }
        ],
    }
    layer = text_layer("#FFFFFF")
    layer.update(
        {
            "name": "门店电话",
            "content": "电话：400-000-0000",
            "position": {"x": 10, "y": 20},
            "size": {"width": 400, "height": 80},
        }
    )
    design = {"canvas": {"width": 1080, "height": 1440}, "layers": [layer]}
    report = quality.assess_scene(design, Image.new("RGB", (1080, 1440), (20, 20, 20)), platform=platform)
    codes = {item["code"] for item in report["hard_failures"]}
    assert "safe_area" in codes
    assert "platform_contact_forbidden" not in codes


def test_contact_coverage_is_advisory_and_not_a_hard_failure():
    platform = {
        "id": "instagram",
        "name": "Instagram",
        "contact_policy": "required",
        "default_placement_id": "portrait",
        "placements": [
            {
                "id": "portrait",
                "name": "竖版帖子",
                "ratio": "4:5",
                "width": 1080,
                "height": 1350,
                "safe_area": {"top": 0.05, "right": 0.05, "bottom": 0.05, "left": 0.05},
            }
        ],
    }
    design = {"canvas": {"width": 1080, "height": 1350}, "layers": []}
    report = quality.assess_scene(
        design,
        Image.new("RGB", (1080, 1350), (20, 20, 20)),
        platform=platform,
        merchant_info={"address": "上海某路 88 号", "phone": "400-000-0000"},
    )
    codes = {item["code"] for item in report["hard_failures"]}
    assert "platform_contact_missing" not in codes


def test_safe_area_clamp_moves_key_layers_inside_platform_bounds():
    placement = {
        "safe_area": {"top": 0.05, "right": 0.05, "bottom": 0.05, "left": 0.05}
    }
    layers = [
        {
            "id": "logo",
            "type": "image",
            "role": "logo",
            "visible": True,
            "position": {"x": 907, "y": 57},
            "size": {"width": 129, "height": 129},
        },
        {
            "id": "title",
            "type": "text",
            "role": "headline",
            "content": "标题",
            "visible": True,
            "position": {"x": 40, "y": 60},
            "size": {"width": 400, "height": 100},
        },
    ]
    clamped = quality.clamp_layers_to_safe_area(
        layers, {"width": 1080, "height": 1440}, placement
    )
    logo, title = clamped
    assert logo["position"] == {"x": 897, "y": 72}
    assert title["position"]["x"] == 54
    assert title["position"]["y"] == 72

    design = {"canvas": {"width": 1080, "height": 1440}, "layers": clamped}
    report = quality.assess_scene(
        design,
        Image.new("RGB", (1080, 1440), (20, 20, 20)),
        platform={
            "id": "xiaohongshu",
            "name": "小红书",
            "contact_policy": "forbidden",
            "placements": [placement],
        },
        placement={"safe_area": placement["safe_area"]},
    )
    assert not any(item["code"] == "safe_area" for item in report["hard_failures"])


