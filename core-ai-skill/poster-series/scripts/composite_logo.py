#!/usr/bin/env python3
"""Composite an official transparent Logo onto a generated poster PNG.

Designed to run directly inside the Core AI skill sandbox using only Python
standard library + Pillow (no backend app or numpy dependency).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps

LOGO_CONTRAST_TARGET = 3.0
LOGO_INK_ALPHA = 96
PLATE_TARGET_COVERAGE = 0.95
PLATE_COLORS = ((26, 26, 28), (250, 250, 247), (119, 119, 119))
PLATE_OPACITIES = (0.82, 0.92, 1.0)


def _srgb_channel_to_linear(channel: float) -> float:
    c = channel / 255.0
    if c <= 0.03928:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def _rgb_luminance(r: float, g: float, b: float) -> float:
    return (
        0.2126 * _srgb_channel_to_linear(r)
        + 0.7152 * _srgb_channel_to_linear(g)
        + 0.0722 * _srgb_channel_to_linear(b)
    )


def _contrast_ratio(first: float, second: float) -> float:
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


def _logo_ink_box(logo: Image.Image) -> tuple[int, int, int, int] | None:
    return logo.getchannel("A").point(lambda a: 255 if a >= LOGO_INK_ALPHA else 0).getbbox()


def _check_logo_size(logo: Image.Image, minimum: int) -> None:
    bounds = _logo_ink_box(logo)
    if not bounds or max(bounds[2] - bounds[0], bounds[3] - bounds[1]) < minimum:
        raise ValueError(f"Logo visible ink size is below minimum {minimum}px")


def _plate_tile(
    width: int, height: int, radius: int, color: tuple[int, int, int], opacity: float
) -> Image.Image:
    scale = 4
    tile = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle(
        (0, 0, width * scale - 1, height * scale - 1),
        radius=max(1, radius * scale),
        fill=(*color, int(round(255 * opacity))),
    )
    return tile.resize((width, height), Image.Resampling.LANCZOS)


def _add_contrast_plate(
    base: Image.Image, logo: Image.Image, x: int, y: int, box: dict[str, Any]
) -> tuple[bool, float]:
    """Measure the actual composite, including mixed light and dark artwork."""
    backdrop = base.crop((x, y, x + logo.width, y + logo.height))
    current_ratio = _check_logo_contrast(backdrop, logo)
    ink_box = _logo_ink_box(logo)
    if ink_box is None or current_ratio >= PLATE_TARGET_COVERAGE:
        return False, current_ratio

    pad = max(2, int(round(min(ink_box[2] - ink_box[0], ink_box[3] - ink_box[1]) * 0.08)))
    box_left = int(box.get("x") or 0)
    box_top = int(box.get("y") or 0)
    left = max(0, box_left, x + ink_box[0] - pad)
    top = max(0, box_top, y + ink_box[1] - pad)
    right = min(base.width, box_left + int(box.get("width") or logo.width), x + ink_box[2] + pad)
    bottom = min(base.height, box_top + int(box.get("height") or logo.height), y + ink_box[3] + pad)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return False, current_ratio

    radius = max(3, int(round(min(width, height) * 0.22)))
    best_tile = None
    best_ratio = current_ratio
    for opacity in PLATE_OPACITIES:
        for color in PLATE_COLORS:
            tile = _plate_tile(width, height, radius, color, opacity)
            candidate = backdrop.copy()
            candidate.alpha_composite(tile, (left - x, top - y))
            ratio = _check_logo_contrast(candidate, logo)
            if ratio > best_ratio:
                best_tile, best_ratio = tile, ratio
        if best_ratio >= PLATE_TARGET_COVERAGE:
            break
    if best_tile is not None:
        base.alpha_composite(best_tile, (left, top))
    return best_tile is not None, best_ratio


def _check_logo_contrast(backdrop: Image.Image, logo: Image.Image) -> float:
    composited = Image.alpha_composite(backdrop.convert("RGBA"), logo.convert("RGBA")).convert("RGB")
    back_rgb = backdrop.convert("RGB")
    weighted_pass = 0.0
    total_weight = 0.0
    for (br, bg, bb), (fr, fg, fb), alpha in zip(
        back_rgb.getdata(), composited.getdata(), logo.getchannel("A").getdata()
    ):
        if alpha < LOGO_INK_ALPHA:
            continue
        w = (alpha / 255.0) ** 2
        total_weight += w
        if _contrast_ratio(_rgb_luminance(br, bg, bb), _rgb_luminance(fr, fg, fb)) >= LOGO_CONTRAST_TARGET:
            weighted_pass += w
    if total_weight == 0.0:
        return 0.0
    return weighted_pass / total_weight


def composite_logo_file(
    base_path: Path,
    logo_path: Path,
    out_path: Path,
    *,
    box: dict[str, Any],
    canvas: tuple[int, int] | None = None,
    min_visible_px: int = 0,
) -> dict[str, Any]:
    with Image.open(base_path) as source:
        base = ImageOps.exif_transpose(source).convert("RGBA")
        target_canvas = canvas or base.size
        if base.size != target_canvas:
            base = ImageOps.fit(base, target_canvas, method=Image.Resampling.LANCZOS)

    with Image.open(logo_path) as source:
        logo = ImageOps.exif_transpose(source).convert("RGBA")
        target_width = max(1, int(box.get("width") or logo.width))
        target_height = max(1, int(box.get("height") or logo.height))
        if min_visible_px:
            logo = ImageOps.contain(logo, (target_width, target_height), Image.Resampling.LANCZOS)
            _check_logo_size(logo, min_visible_px)
        else:
            logo.thumbnail((target_width, target_height), Image.Resampling.LANCZOS)

        x = max(0, int(box.get("x") or 0) + (target_width - logo.width) // 2)
        y = max(0, int(box.get("y") or 0) + (target_height - logo.height) // 2)
        plate_applied, contrast_pass_ratio = _add_contrast_plate(base, logo, x, y, box)
        base.alpha_composite(logo, (x, y))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    base.convert("RGB").save(out_path, format="PNG", optimize=True)

    # Re-open to verify output integrity
    with Image.open(out_path) as verified:
        verified.load()
        out_size = verified.size

    return {
        "output": str(out_path),
        "canvas": {"width": out_size[0], "height": out_size[1]},
        "logo_box": {
            "x": int(box.get("x") or 0),
            "y": int(box.get("y") or 0),
            "width": target_width,
            "height": target_height,
        },
        "placed_rect": {"x": x, "y": y, "width": logo.width, "height": logo.height},
        "plate_applied": plate_applied,
        "contrast_pass_ratio": round(contrast_pass_ratio, 4),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Composite an official transparent Logo onto a generated poster PNG."
    )
    parser.add_argument("--base", required=True, type=Path, help="Input no-Logo poster PNG")
    parser.add_argument("--logo", required=True, type=Path, help="Official transparent Logo PNG")
    parser.add_argument("--out", required=True, type=Path, help="Output final PNG path")
    parser.add_argument("--box", help='JSON object {"x":..,"y":..,"width":..,"height":..}')
    parser.add_argument("--x", type=int, help="Logo box X coordinate")
    parser.add_argument("--y", type=int, help="Logo box Y coordinate")
    parser.add_argument("--width", type=int, help="Logo box width")
    parser.add_argument("--height", type=int, help="Logo box height")
    parser.add_argument("--canvas-width", type=int, help="Exact output canvas width")
    parser.add_argument("--canvas-height", type=int, help="Exact output canvas height")
    parser.add_argument(
        "--min-visible-px",
        type=int,
        default=0,
        help="Optional minimum visible Logo ink size in pixels",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.box:
        box = json.loads(args.box)
    elif None not in (args.x, args.y, args.width, args.height):
        box = {"x": args.x, "y": args.y, "width": args.width, "height": args.height}
    else:
        raise SystemExit("Provide --box JSON or all of --x --y --width --height")

    canvas = None
    if args.canvas_width and args.canvas_height:
        canvas = (args.canvas_width, args.canvas_height)

    min_visible = args.min_visible_px or int(box.get("min_visible_px") or 0)
    result = composite_logo_file(
        args.base,
        args.logo,
        args.out,
        box=box,
        canvas=canvas,
        min_visible_px=min_visible,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
