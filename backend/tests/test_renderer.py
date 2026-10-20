from __future__ import annotations

from PIL import Image, ImageChops

from app.services import design as design_utils
from app.services.renderer import compose_poster, render_design


def make_background(size=(900, 900)) -> Image.Image:
    image = Image.new("RGB", size, (90, 55, 35))
    for x in range(size[0]):
        for y in range(0, size[1], 20):
            image.putpixel((x, y), (110 + x % 40, 65, 40))
    return image


def test_compose_outputs_square_png_with_logo(settings):
    logo = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
    for x in range(20, 100):
        for y in range(20, 100):
            logo.putpixel((x, y), (180, 20, 20, 255))
    result = compose_poster(settings, make_background(), logo)
    assert result.image.size == (1024, 1024)
    assert result.image.mode == "RGB"
    assert result.design["logo_box"]["width"] == 220


def test_empty_logo_keeps_photo_clean(settings):
    result = compose_poster(settings, make_background((700, 1000)), None)
    assert result.image.size == (1024, 1024)
    assert result.design["logo_box"] is None


def test_portrait_canvas(settings):
    result = compose_poster(
        settings, make_background((720, 1280)), None, canvas_size=(1080, 1920)
    )
    assert result.image.size == (1080, 1920)


def test_visual_component_blend_and_shadow_metadata(settings):
    background = Image.new("RGB", (240, 240), (100, 100, 100))
    patch = design_utils.make_shape_layer(
        layer_id="patch", name="色块", shape="rect",
        box={"x": 0, "y": 0, "width": 120, "height": 120},
        fill="#FF0000", z_index=1,
    )
    component = design_utils.make_visual_component(
        component_id="component_blend", name="融合组件", semantic_type="offer_badge",
        content={"text": "融合"}, children=[patch],
        box={"x": 40, "y": 40, "width": 120, "height": 120},
        compositing={
            "blend_mode": "multiply", "opacity": 1.0, "feather_px": 0,
            "shadow": {"blur": 6, "offset_x": 0, "offset_y": 4, "color": "#00000040"},
        },
    )
    result = render_design(settings, background, [component], (240, 240))
    pixel = result.image.getpixel((100, 100))
    assert pixel[0] > pixel[1]
    assert pixel[1] < 20
    metrics = [item for item in result.design["layers"] if item["type"] == "visual_component"]
    assert metrics[0]["compositing"]["blend_mode"] == "multiply"


def test_visual_component_feather_does_not_blur_native_text(settings):
    background = Image.new("RGB", (360, 220), (20, 20, 20))
    text = design_utils.make_text_layer(
        layer_id="title_text",
        name="标题",
        role="headline",
        content="清晰标题",
        box={"x": 20, "y": 20, "width": 260, "height": 100},
        style={"font_family": "sans", "font_size": 64, "color": "#FFFFFF"},
    )

    def render_with_feather(value: float):
        component = design_utils.make_visual_component(
            component_id="title_component",
            name="标题组件",
            semantic_type="headline",
            content={"text": "清晰标题"},
            children=[text],
            box={"x": 20, "y": 40, "width": 300, "height": 140},
            compositing={"blend_mode": "normal", "opacity": 1.0, "feather_px": value},
        )
        return render_design(settings, background, [component], (360, 220)).image

    crisp = render_with_feather(0)
    feathered = render_with_feather(5)
    assert ImageChops.difference(crisp, feathered).getbbox() is None
