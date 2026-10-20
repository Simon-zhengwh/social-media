from __future__ import annotations

import math

MIN_GATEWAY_PIXELS = 1024 * 1024

CANVAS_SIZES: dict[str, tuple[int, int]] = {
    "1:1": (1024, 1024),
    "9:16": (1080, 1920),
    "4:5": (1080, 1350),
    "16:9": (1920, 1080),
    "3:4": (1080, 1440),
    "4:3": (1440, 1080),
}

RATIO_RESOLUTIONS: dict[str, str] = {
    "1:1": "1K",
    "9:16": "2K",
    "4:5": "2K",
    "16:9": "2K",
    "3:4": "2K",
    "4:3": "2K",
}


def canvas_size(ratio: str) -> tuple[int, int]:
    return CANVAS_SIZES.get(ratio, CANVAS_SIZES["1:1"])


def resolution_for(ratio: str) -> str:
    return RATIO_RESOLUTIONS.get(ratio, "1K")


def gateway_image_size(width: int, height: int, max_side: int = 3840) -> str:
    """Return an OpenAI-style WxH size with both edges divisible by 16.

    The image provider rejects tiny edits (e.g. a 176x128 logo crop) with
    "Requested resolution is below the current minimum pixel budget", so small
    regions are scaled up while keeping the aspect ratio close to the source.
    """
    width = max(16, int(width))
    height = max(16, int(height))
    scale = min(1.0, max_side / max(width, height))
    if width * height * scale * scale < MIN_GATEWAY_PIXELS:
        scale = min(
            math.sqrt(MIN_GATEWAY_PIXELS / (width * height)),
            max_side / max(width, height),
        )
    scaled_width = width * scale
    scaled_height = height * scale
    if scale > 1.0:
        scaled_width = math.ceil(scaled_width / 16) * 16
        scaled_height = math.ceil(scaled_height / 16) * 16
    else:
        scaled_width = max(16, int(scaled_width) // 16 * 16)
        scaled_height = max(16, int(scaled_height) // 16 * 16)
    overflow = max(scaled_width, scaled_height) - max_side
    if overflow > 0:
        shrink = max_side / max(scaled_width, scaled_height)
        scaled_width = max(16, int(scaled_width * shrink) // 16 * 16)
        scaled_height = max(16, int(scaled_height * shrink) // 16 * 16)
    return f"{scaled_width}x{scaled_height}"
