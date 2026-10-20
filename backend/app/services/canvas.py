from __future__ import annotations

CANVAS_SIZES: dict[str, tuple[int, int]] = {
    "1:1": (1024, 1024),
    "9:16": (1080, 1920),
    "4:5": (1080, 1350),
    "16:9": (1920, 1080),
}

RATIO_RESOLUTIONS: dict[str, str] = {
    "1:1": "1K",
    "9:16": "2K",
    "4:5": "2K",
    "16:9": "2K",
}


def canvas_size(ratio: str) -> tuple[int, int]:
    return CANVAS_SIZES.get(ratio, CANVAS_SIZES["1:1"])


def resolution_for(ratio: str) -> str:
    return RATIO_RESOLUTIONS.get(ratio, "1K")
