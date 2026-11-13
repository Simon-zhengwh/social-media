"""Reference-image encoding shared by every image model.

The gateway takes base64 data URLs in `b64Json`; every reference image needs the same
"shrink until it fits" behaviour, so the encoding lives here instead of inside a client.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image, ImageOps

MAX_REFERENCE_BYTES = 1_500_000
MAX_TOTAL_REFERENCE_BYTES = 8_000_000


def reference_data_url(path: Path, *, keep_alpha: bool | None = None) -> str:
    """Encode one local image as a data URI, keeping transparency when it matters."""
    try:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
            has_alpha = (
                "A" in image.getbands()
                and image.convert("RGBA").getchannel("A").getextrema()[0] < 250
            )
            if keep_alpha is None:
                keep_alpha = has_alpha
            image = image.convert("RGBA" if keep_alpha else "RGB")
    except Exception as exc:  # noqa: BLE001 - provider input is untrusted
        from app.services.core_ai import CoreAIError

        raise CoreAIError("invalid_reference", f"参考图无法读取：{path.name}", 422) from exc

    if keep_alpha:
        data, mime = encode_png(image), "image/png"
    else:
        data, mime = encode_jpeg(image), "image/jpeg"
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def encode_jpeg(image: Image.Image) -> bytes:
    current = image.copy()
    current.thumbnail((1536, 1536), Image.Resampling.LANCZOS)
    for _ in range(6):
        for quality in (88, 82, 76, 70, 64):
            buffer = io.BytesIO()
            current.save(buffer, format="JPEG", quality=quality, optimize=True)
            data = buffer.getvalue()
            if len(data) <= MAX_REFERENCE_BYTES:
                return data
        current.thumbnail(
            (max(256, int(current.width * 0.85)), max(256, int(current.height * 0.85))),
            Image.Resampling.LANCZOS,
        )
    buffer = io.BytesIO()
    current.save(buffer, format="JPEG", quality=60, optimize=True)
    return buffer.getvalue()


def encode_png(image: Image.Image) -> bytes:
    current = image.copy()
    current.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
    for _ in range(6):
        buffer = io.BytesIO()
        current.save(buffer, format="PNG", optimize=True)
        data = buffer.getvalue()
        if len(data) <= MAX_REFERENCE_BYTES:
            return data
        current.thumbnail(
            (max(128, int(current.width * 0.8)), max(128, int(current.height * 0.8))),
            Image.Resampling.LANCZOS,
        )
    return data
