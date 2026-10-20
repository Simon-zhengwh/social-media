from __future__ import annotations

import hashlib
import io
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import Settings
from app.services.object_store import ObjectStore

ALLOWED_FORMATS = {
    "JPEG": (".jpg", "image/jpeg"),
    "PNG": (".png", "image/png"),
    "WEBP": (".webp", "image/webp"),
}
STORAGE_SUBDIRS = ("originals", "prepared", "backgrounds", "outputs", "logs")


class StorageError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass
class PreparedUpload:
    id: str
    kind: str
    original_name: str
    path: str
    prepared_path: str
    mime_type: str
    width: int
    height: int
    bytes: int
    sha256: str


def ensure_storage_dirs(settings: Settings) -> None:
    for name in STORAGE_SUBDIRS:
        (settings.storage_path / name).mkdir(parents=True, exist_ok=True)


def resolve_storage_path(settings: Settings, relative: str) -> Path:
    root = settings.storage_path.resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise StorageError("invalid_path", "非法的文件路径")
    return candidate


def object_key(settings: Settings, subdir: str, name: str) -> str:
    if subdir not in {"originals", "prepared", "backgrounds", "outputs"}:
        raise StorageError("invalid_subdir", "非法的对象目录")
    if not name.endswith(".png") and subdir != "originals":
        name = f"{name}.png"
    prefix = settings.cos_prefix.strip("/") or "social-media"
    return f"{prefix}/{subdir}/{name}"


def encode_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def prepare_upload(
    settings: Settings,
    store: ObjectStore,
    data: bytes,
    original_name: str,
    kind: str,
) -> PreparedUpload:
    if kind not in {"product", "logo", "decoration", "brand_reference"}:
        raise StorageError("invalid_kind", "只支持 product、logo、decoration 或 brand_reference 素材")
    if not data:
        raise StorageError("empty_file", "文件为空")
    if len(data) > settings.max_upload_bytes:
        raise StorageError("file_too_large", "文件超过 10 MB 限制")

    try:
        with Image.open(io.BytesIO(data)) as probe:
            image_format = probe.format
            width, height = probe.size
            probe.verify()
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise StorageError("invalid_image", "无法识别的图片文件") from exc

    if image_format not in ALLOWED_FORMATS:
        raise StorageError("unsupported_format", "仅支持 JPEG、PNG、WebP")
    if width <= 0 or height <= 0 or width * height > settings.max_image_pixels:
        raise StorageError("image_too_large", "图片像素超过 2500 万限制")

    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
            prepared = image.convert("RGBA" if kind in {"logo", "decoration"} else "RGB")
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise StorageError("invalid_image", "图片解码失败") from exc

    max_side = 2048 if kind == "product" else 1600 if kind == "brand_reference" else 1024
    prepared.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

    if kind in {"logo", "decoration"} and prepared.mode == "RGBA":
        alpha = prepared.getchannel("A")
        if alpha.getextrema()[1] < 16:
            raise StorageError("blank_logo" if kind == "logo" else "blank_asset", "图片几乎全透明，请检查文件")

    asset_id = f"asset_{uuid.uuid4().hex}"
    extension, mime_type = ALLOWED_FORMATS[image_format]
    original_key = object_key(settings, "originals", f"{asset_id}{extension}")
    prepared_key = object_key(settings, "prepared", f"{asset_id}.png")
    store.put_bytes(original_key, data, mime_type)
    store.put_bytes(prepared_key, encode_png(prepared), "image/png")

    return PreparedUpload(
        id=asset_id,
        kind=kind,
        original_name=original_name or f"upload{extension}",
        path=original_key,
        prepared_path=prepared_key,
        mime_type=mime_type,
        width=prepared.width,
        height=prepared.height,
        bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def save_generated_png(
    store: ObjectStore,
    settings: Settings,
    subdir: str,
    name: str,
    image: Image.Image,
) -> str:
    key = object_key(settings, subdir, name)
    store.put_bytes(key, encode_png(image), "image/png")
    return key


def save_raw_image(
    store: ObjectStore,
    settings: Settings,
    subdir: str,
    name: str,
    data: bytes,
) -> str:
    key = object_key(settings, subdir, name)
    store.put_bytes(key, data, "image/png")
    return key


def open_image(store: ObjectStore, key: str) -> Image.Image:
    data = store.get_bytes(key)
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            return image.copy()
    except (UnidentifiedImageError, OSError) as exc:
        raise StorageError("invalid_image", "对象中的图片无法解码", status_code=502) from exc
