from __future__ import annotations

from app.services.canvas import MIN_GATEWAY_PIXELS, canvas_size, gateway_image_size


def parse(size: str) -> tuple[int, int]:
    width, height = size.split("x")
    return int(width), int(height)


def test_gateway_size_matches_canvas_examples():
    assert gateway_image_size(1080, 1920) == "1072x1920"
    assert gateway_image_size(1080, 1350) == "1072x1344"
    assert gateway_image_size(1024, 1024) == "1024x1024"


def test_tiny_logo_crop_is_scaled_above_pixel_budget():
    width, height = parse(gateway_image_size(176, 128))
    assert width * height >= MIN_GATEWAY_PIXELS
    assert width % 16 == 0
    assert height % 16 == 0
    assert abs(width / height - 176 / 128) < 0.05


def test_large_crop_is_capped_at_max_side():
    width, height = parse(gateway_image_size(5000, 1000))
    assert max(width, height) <= 3840
    assert width % 16 == 0
    assert height % 16 == 0


def test_platform_ratios_have_export_canvases_and_aligned_gateway_sizes():
    assert canvas_size("3:4") == (1080, 1440)
    assert canvas_size("4:3") == (1440, 1080)
    assert gateway_image_size(1080, 1440) == "1072x1440"
    assert gateway_image_size(1440, 1080) == "1440x1072"
