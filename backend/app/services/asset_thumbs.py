from __future__ import annotations

import io
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import Settings
from app.services.object_store import ObjectStore

logger = logging.getLogger(__name__)

DEFAULT_WIDTH = 480
MIN_WIDTH = 160
MAX_WIDTH = 1024
QUALITY = 78
# Task cards render at ~207x255 CSS px, so 480 keeps them sharp on a 2x screen while
# cutting a 1080x1350 PNG from ~2 MB to a few tens of KB.
MAX_SOURCE_PIXELS = 80_000_000


class ThumbError(Exception):
    """A thumbnail cannot be produced for this asset."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def clamp_width(width: int | None) -> int:
    if width is None:
        return DEFAULT_WIDTH
    return max(MIN_WIDTH, min(MAX_WIDTH, int(width)))


def cache_path(settings: Settings, asset_id: str, width: int) -> Path:
    return settings.storage_path / "thumbs" / f"{asset_id}.w{width}.webp"


def thumb_bytes(
    settings: Settings,
    store: ObjectStore,
    asset: dict[str, Any],
    width: int | None = None,
) -> bytes:
    """Return a WebP thumbnail for one asset, caching it on local scratch disk.

    The cache lives under STORAGE_DIR, which the API Pod mounts as an emptyDir: a restart
    drops it and the next request rebuilds the handful of visible cards. Nothing is written
    to AM media storage, so the media-object kind contract stays untouched.
    """
    asset_id = str(asset["_id"])
    resolved = clamp_width(width)
    path = cache_path(settings, asset_id, resolved)
    if path.is_file():
        try:
            return path.read_bytes()
        except OSError:
            logger.warning("thumbnail cache unreadable; regenerating: %s", path)

    relative = asset.get("prepared_path") or asset.get("path")
    if not relative:
        raise ThumbError(404, "素材文件不存在")
    data = _read_source(store, str(relative))
    rendered = render_thumb(data, resolved)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".tmp-{uuid.uuid4().hex}"
        try:
            tmp.write_bytes(rendered)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    except OSError:
        # Serving the bytes matters more than keeping the cache.
        logger.warning("thumbnail cache write failed: %s", path)
    return rendered


def render_thumb(data: bytes, width: int) -> bytes:
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > MAX_SOURCE_PIXELS:
                raise ThumbError(413, "图片像素过大，无法生成缩略图")
            image = ImageOps.exif_transpose(source)
            has_alpha = "A" in image.getbands() or "transparency" in image.info
            prepared = image.convert("RGBA" if has_alpha else "RGB")
    except ThumbError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ThumbError(415, "无法生成缩略图") from exc

    if prepared.width > width:
        height = max(1, round(prepared.height * (width / prepared.width)))
        prepared = prepared.resize((width, height), Image.Resampling.LANCZOS)

    buffer = io.BytesIO()
    prepared.save(buffer, format="WEBP", quality=QUALITY, method=4)
    return buffer.getvalue()


def _read_source(store: ObjectStore, relative: str) -> bytes:
    local_path = store.local_path(relative)
    if local_path is not None:
        return local_path.read_bytes()
    return store.get_bytes(relative)
