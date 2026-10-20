from __future__ import annotations

import copy

from PIL import Image

from app.services.renderer import compose_poster, render_design


def solid(size=(400, 300), color=(20, 40, 60)) -> Image.Image:
    return Image.new("RGB", size, color)


def transparent_logo() -> Image.Image:
    logo = Image.new("RGBA", (80, 80), (0, 0, 0, 0))
    for x in range(25, 55):
        for y in range(25, 55):
            logo.putpixel((x, y), (220, 30, 30, 255))
    return logo


def text_layer(**overrides):
    layer = {
        "id": "text_headline",
        "type": "text",
        "name": "主标题",
        "role": "headline",
        "content": "周末限时特惠",
        "position": {"x": 40, "y": 40},
        "size": {"width": 320, "height": 120},
        "style": {
            "font_size": 64,
            "font_weight": 900,
            "color": "#FFFFFF",
            "align": "left",
            "line_height": 1.05,
            "letter_spacing": 0,
            "opacity": 1.0,
            "stroke": {"width": 0, "color": "#000000"},
            "shadow": {"blur": 0, "offset_x": 0, "offset_y": 0, "color": "#00000000"},
            "background": None,
        },
        "z_index": 10,
        "visible": True,
    }
    layer.update(overrides)
    return layer


def test_transparent_logo_has_no_opaque_plate(settings):
    background = solid()
    logo = transparent_logo()
    layer = {
        "id": "logo_brand",
        "type": "logo",
        "asset_id": "logo",
        "position": {"x": 100, "y": 60},
        "size": {"width": 80, "height": 80},
        "opacity": 1.0,
        "rotation": 0,
        "mode": "overlay",
        "z_index": 10,
        "visible": True,
    }
    result = render_design(
        settings,
        background,
        [layer],
        (400, 300),
        assets={"logo": logo},
    )
    # Transparent margin inside the logo box must show the original background,
    # proving there is no white/cream card behind it.
    assert result.image.getpixel((105, 65)) == (20, 40, 60)
    assert result.image.getpixel((140, 100)) == (220, 30, 30)
    assert result.image.getpixel((300, 250)) == (20, 40, 60)


def test_brush_font_family_renders_with_brush_metrics(settings):
    background = solid((500, 320), (18, 36, 28))
    layer = text_layer()
    layer["style"]["font_family"] = "brush"
    layer["style"]["font_size"] = 96
    layer["position"] = {"x": 40, "y": 80}
    layer["size"] = {"width": 420, "height": 180}
    result = render_design(
        settings,
        background,
        [layer],
        (500, 320),
        assets={},
    )
    metrics = [item for item in result.design["layers"] if item["type"] == "text"]
    assert metrics[0]["font_family"] == "brush"
    assert result.image.getbbox() is not None


def test_logo_overlay_is_pixel_identical_outside_alpha(settings):
    background = solid((200, 200), (12, 34, 56))
    logo = transparent_logo()
    layer = {
        "id": "logo_brand",
        "type": "logo",
        "asset_id": "logo",
        "position": {"x": 20, "y": 20},
        "size": {"width": 80, "height": 80},
        "opacity": 1.0,
        "rotation": 0,
        "mode": "overlay",
        "z_index": 10,
        "visible": True,
    }
    result = render_design(
        settings, background, [layer], (200, 200), assets={"logo": logo}
    )
    assert result.image.getpixel((10, 10)) == (12, 34, 56)
    assert result.image.getpixel((30, 30)) == (12, 34, 56)
    assert result.image.getpixel((50, 50)) == (220, 30, 30)


def test_text_renders_and_autofits_long_chinese(settings):
    content = "周末限时特惠全场菜品五折起欢迎到店品尝"
    layer = text_layer(content=content, size={"width": 260, "height": 120})
    before = copy.deepcopy(layer)
    result = render_design(settings, solid((400, 300)), [layer], (400, 300))
    assert layer == before, "renderer must not mutate the design layer"
    metrics = result.design["layers"][0]
    assert metrics["type"] == "text"
    assert metrics["used_font_size"] <= 64
    assert "".join(metrics["lines"]).replace(" ", "") == content
    assert metrics["overflow"] is False
    changed = sum(
        1
        for x in range(40, 300)
        for y in range(40, 160)
        if result.image.getpixel((x, y)) != (20, 40, 60)
    )
    assert changed > 500


def test_explicit_newline_and_alignment(settings):
    layer = text_layer(
        content="周末\n特惠",
        style={
            "font_size": 60,
            "font_weight": 900,
            "color": "#FFFFFF",
            "align": "center",
            "line_height": 1.0,
            "letter_spacing": 0,
            "opacity": 1.0,
            "stroke": {"width": 0, "color": "#000000"},
            "shadow": {"blur": 0, "offset_x": 0, "offset_y": 0, "color": "#00000000"},
            "background": None,
        },
    )
    result = render_design(settings, solid((400, 300)), [layer], (400, 300))
    metrics = result.design["layers"][0]
    assert metrics["lines"] == ["周末", "特惠"]
    assert metrics["bounds"]["x"] > 40


def test_z_index_order(settings):
    bottom = text_layer(
        id="bottom",
        content="████",
        position={"x": 20, "y": 20},
        size={"width": 300, "height": 100},
        style={
            "font_size": 80,
            "font_weight": 900,
            "color": "#FF0000",
            "align": "left",
            "line_height": 1.0,
            "letter_spacing": 0,
            "opacity": 1.0,
            "stroke": {"width": 0, "color": "#000000"},
            "shadow": {"blur": 0, "offset_x": 0, "offset_y": 0, "color": "#00000000"},
            "background": None,
        },
        z_index=10,
    )
    top = copy.deepcopy(bottom)
    top["id"] = "top"
    top["z_index"] = 20
    top["style"]["color"] = "#00FF00"
    result = render_design(settings, solid((400, 300)), [bottom, top], (400, 300))
    pixel = result.image.getpixel((60, 60))
    assert pixel[1] > pixel[0], pixel


def test_compose_poster_has_no_badge(settings):
    background = solid((300, 300), (90, 60, 30))
    logo = transparent_logo()
    result = compose_poster(settings, background, logo)
    assert result.image.size == (settings.canvas_width, settings.canvas_height)
    # The old code painted a cream rounded rectangle here. Now the only change
    # from the background should be the transparent logo itself.
    corner = result.image.getpixel((5, 5))
    assert corner == (90, 60, 30)


def test_rotated_text_renders_and_records_rotation(settings):
    layer = text_layer(rotation=9)
    result = render_design(
        settings,
        solid((400, 300)),
        [layer],
        (400, 300),
    )
    metrics = result.design["layers"][0]
    assert metrics["rotation"] == 9
    assert "rotated_bounds" in metrics
    changed = sum(
        1
        for x in range(0, 400)
        for y in range(0, 300)
        if result.image.getpixel((x, y)) != (20, 40, 60)
    )
    assert changed > 500
