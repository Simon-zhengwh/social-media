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
