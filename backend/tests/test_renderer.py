from __future__ import annotations

from PIL import Image

from app.services.renderer import compose_poster


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
