from __future__ import annotations

import hashlib
import io
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

register_heif_opener()

from app.config import Settings
from app.services.object_store import ObjectStore

ALLOWED_FORMATS = {
    "JPEG": (".jpg", "image/jpeg"),
    "MPO": (".jpg", "image/jpeg"),
    "PNG": (".png", "image/png"),
    "APNG": (".png", "image/apng"),
    "WEBP": (".webp", "image/webp"),
    "GIF": (".gif", "image/gif"),
    "BMP": (".bmp", "image/bmp"),
    "TIFF": (".tiff", "image/tiff"),
    "AVIF": (".avif", "image/avif"),
    "HEIF": (".heic", "image/heic"),
    "JPEG2000": (".jp2", "image/jp2"),
    "ICO": (".ico", "image/x-icon"),
    "TGA": (".tga", "image/x-tga"),
}
SUPPORTED_IMAGE_SUFFIXES = frozenset(
    {
        ".jpg", ".jpeg", ".jfif", ".png", ".apng", ".webp", ".gif",
        ".bmp", ".dib", ".tif", ".tiff", ".avif", ".avifs", ".heic",
        ".heif", ".jp2", ".j2k", ".jpf", ".jpx", ".ico", ".tga",
        ".vda", ".vst",
    }
)
SUPPORTED_IMAGE_LABEL = (
    "JPEG、PNG、WebP、GIF、BMP、TIFF、AVIF、HEIC/HEIF、JPEG 2000、ICO、TGA"
)
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


def _upload_descriptor(image_format: str, original_name: str) -> tuple[str, str]:
    if image_format == "HEIF":
        suffix = Path(original_name).suffix.lower()
        if suffix == ".heif":
            return ".heif", "image/heif"
    return ALLOWED_FORMATS[image_format]


def prepare_upload(
    settings: Settings,
    store: ObjectStore,
    data: bytes,
    original_name: str,
) -> PreparedUpload:
    if not data:
        raise StorageError("empty_file", "文件为空")
    if len(data) > settings.max_upload_bytes:
        raise StorageError("file_too_large", f"文件超过 {settings.max_upload_bytes / 1_000_000:g} MB 限制")

    try:
        with Image.open(io.BytesIO(data)) as probe:
            image_format = probe.format
            width, height = probe.size
            probe.verify()
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise StorageError("invalid_image", "无法识别的图片文件") from exc

    if image_format not in ALLOWED_FORMATS:
        raise StorageError("unsupported_format", f"仅支持 {SUPPORTED_IMAGE_LABEL}")
    pixel_count = width * height
    if width <= 0 or height <= 0 or pixel_count > settings.max_image_pixels:
        raise StorageError(
            "image_too_large",
            f"图片像素超过 {settings.max_image_pixels / 10_000:g} 万安全上限",
        )

    needs_compression = (
        len(data) > settings.image_compress_threshold_bytes
        or pixel_count > settings.image_downscale_pixel_threshold
    )
    max_side = 2048
    if needs_compression:
        max_side = min(max_side, 1800)

    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            has_alpha = "A" in image.getbands() or "transparency" in image.info
            prepared = image.convert("RGBA" if has_alpha else "RGB")
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        Image.DecompressionBombError,
    ) as exc:
        raise StorageError("invalid_image", "图片解码失败") from exc

    if prepared.mode == "RGBA":
        alpha = prepared.getchannel("A")
        if alpha.getextrema()[1] < 16:
            raise StorageError("blank_asset", "图片几乎全透明，请检查文件")

    asset_id = f"asset_{uuid.uuid4().hex}"
    extension, mime_type = _upload_descriptor(image_format, original_name)
    original_key = object_key(settings, "originals", f"{asset_id}{extension}")
    prepared_key = object_key(settings, "prepared", f"{asset_id}.png")
    store.put_bytes(original_key, data, mime_type)
    store.put_bytes(prepared_key, encode_png(prepared), "image/png")

    return PreparedUpload(
        id=asset_id,
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
