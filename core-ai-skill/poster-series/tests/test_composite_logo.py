from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "composite_logo.py"


def _make_logo(path: Path, color: tuple[int, int, int, int], size: tuple[int, int] = (200, 200)) -> None:
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((20, 20, size[0] - 20, size[1] - 20), fill=color)
    img.save(path, format="PNG")


def _make_base(path: Path, color: tuple[int, int, int], size: tuple[int, int] = (540, 675)) -> None:
    img = Image.new("RGB", size, color)
    img.save(path, format="PNG")


def test_high_contrast_logo_needs_no_plate_and_resizes_to_canvas(tmp_path: Path) -> None:
    base = tmp_path / "raw.png"
    logo = tmp_path / "logo.png"
    out = tmp_path / "final.png"
    _make_base(base, (20, 24, 30), size=(540, 675))
    _make_logo(logo, (250, 250, 250, 255))

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--base",
            str(base),
            "--logo",
            str(logo),
            "--out",
            str(out),
            "--canvas-width",
            "1080",
            "--canvas-height",
            "1350",
            "--box",
            json.dumps({"x": 900, "y": 60, "width": 100, "height": 100}),
            "--min-visible-px",
            "40",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(proc.stdout)
    assert payload["canvas"] == {"width": 1080, "height": 1350}
    assert payload["plate_applied"] is False
    assert payload["contrast_pass_ratio"] >= 0.99

    with Image.open(out) as result:
        assert result.size == (1080, 1350)
        # Center of the logo circle should be bright white
        r, g, b = result.getpixel((950, 110))
        assert (r, g, b) == (250, 250, 250)


def test_low_contrast_logo_adds_backing_plate(tmp_path: Path) -> None:
    base = tmp_path / "raw.png"
    logo = tmp_path / "logo.png"
    out = tmp_path / "final.png"
    _make_base(base, (250, 248, 244), size=(1080, 1350))
    _make_logo(logo, (248, 248, 248, 255))

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--base",
            str(base),
            "--logo",
            str(logo),
            "--out",
            str(out),
            "--box",
            json.dumps({"x": 60, "y": 60, "width": 100, "height": 100}),
            "--min-visible-px",
            "40",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(proc.stdout)
    assert payload["plate_applied"] is True
    assert payload["contrast_pass_ratio"] >= 0.75


def test_mixed_black_and_white_logo_keeps_both_colors_readable(tmp_path: Path) -> None:
    base = tmp_path / "raw.png"
    logo = tmp_path / "logo.png"
    out = tmp_path / "final.png"
    _make_base(base, (247, 236, 220), size=(320, 400))
    artwork = Image.new("RGBA", (150, 60), (0, 0, 0, 0))
    draw = ImageDraw.Draw(artwork)
    draw.rectangle((10, 10, 85, 49), fill=(255, 255, 255, 255))
    draw.rectangle((96, 10, 139, 49), fill=(0, 0, 0, 255))
    artwork.save(logo)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", str(base), "--logo", str(logo), "--out", str(out),
         "--box", json.dumps({"x": 30, "y": 30, "width": 150, "height": 60}),
         "--min-visible-px", "120"],
        check=True, capture_output=True, text=True,
    )

    assert json.loads(result.stdout)["contrast_pass_ratio"] >= 0.95
    with Image.open(out) as final:
        assert final.getpixel((55, 59)) == (255, 255, 255)
        assert final.getpixel((140, 59)) == (0, 0, 0)
        assert final.getpixel((10, 10)) == (247, 236, 220)
