from __future__ import annotations

import hashlib
import io
import uuid
from collections.abc import Callable
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
STORAGE_SUBDIRS = ("originals", "prepared", "backgrounds", "outputs", "artifacts", "logs")


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
    url: str | None = None
    prepared_url: str | None = None
    # Set only when the stored file was re-encoded to stay under the upload threshold.
    stored_bytes: int = 0
    recompressed: bool = False


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
    if subdir not in {"originals", "prepared", "backgrounds", "outputs", "artifacts"}:
        raise StorageError("invalid_subdir", "非法的对象目录")
    if subdir not in {"originals", "artifacts"} and not name.endswith(".png"):
        name = f"{name}.png"
    prefix = settings.cos_prefix.strip("/") or "social-media"
    return f"{prefix}/{subdir}/{name}"


def encode_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# Re-encoding ladder for oversized uploads and the AM copy: keep the original pixels and
# only shrink when quality alone cannot reach the target.
JPEG_QUALITIES = (92, 88, 84, 80, 76, 72, 68, 64)
JPEG_MIN_SIDE = 256
JPEG_SHRINK_STEPS = 6
# Transparency and animation cannot survive a JPEG re-encode, so those keep their bytes.
ALPHA_FORMATS = {"PNG", "APNG", "GIF", "WEBP", "TIFF", "AVIF", "HEIF"}
# MPO is a phone JPEG with an extra preview frame, so only these formats really animate.
ANIMATED_FORMATS = {"GIF", "APNG", "WEBP", "TIFF"}


@dataclass(frozen=True)
class FittedUpload:
    """Bytes that fit a receiver's request body, with the metadata describing them."""

    data: bytes
    mime_type: str
    filename: str
    recompressed: bool


@dataclass(frozen=True)
class StoredOriginal:
    """The file actually written to the originals/ key, which may be a re-encoded copy."""

    data: bytes
    extension: str
    mime_type: str
    recompressed: bool


def base64_body_size(size: int) -> int:
    """Bytes the same payload occupies once base64-encoded."""
    return 4 * ((size + 2) // 3)


def compress_large_upload(
    data: bytes,
    threshold_bytes: int,
    *,
    image_format: str,
    original_name: str,
    has_alpha: bool,
    animated: bool,
) -> StoredOriginal:
    """Cap an upload above the threshold by re-encoding it, keeping its pixel size when possible.

    Transparency, animation and already-small files keep their bytes. When even the smallest
    JPEG is still too large the original is stored unchanged instead of failing the upload.
    """
    extension, mime_type = _upload_descriptor(image_format, original_name)
    oversized = threshold_bytes > 0 and len(data) > threshold_bytes
    if not oversized or animated or (has_alpha and image_format in ALPHA_FORMATS):
        return StoredOriginal(data=data, extension=extension, mime_type=mime_type, recompressed=False)
    encoded = _encode_jpeg_within(_flatten_for_jpeg(data), threshold_bytes, lambda size: size)
    if encoded is None:
        return StoredOriginal(data=data, extension=extension, mime_type=mime_type, recompressed=False)
    return StoredOriginal(data=encoded, extension=".jpg", mime_type="image/jpeg", recompressed=True)


def fit_upload_to_body(
    data: bytes,
    max_body_bytes: int,
    original_name: str,
    mime_type: str,
    *,
    force_jpeg: bool = False,
) -> FittedUpload:
    """Re-encode to JPEG only when the base64 body would overflow the receiver's limit.

    Callers keep their own original untouched; this only shapes the copy sent upstream.
    `force_jpeg` is for receivers that store no container other than JPEG, PNG and GIF.
    """
    if not force_jpeg and (max_body_bytes <= 0 or base64_body_size(len(data)) <= max_body_bytes):
        return FittedUpload(data=data, mime_type=mime_type, filename=original_name, recompressed=False)

    encoded = _encode_jpeg_within(_flatten_for_jpeg(data), max_body_bytes, base64_body_size)
    if encoded is None:
        raise StorageError(
            "file_too_large",
            f"图片压缩后仍超过 {max_body_bytes / 1_000_000:g} MB 请求上限，请先压缩后再上传",
        )
    return FittedUpload(
        data=encoded,
        mime_type="image/jpeg",
        filename=_jpeg_name(original_name),
        recompressed=True,
    )


def _encode_jpeg_within(image: Image.Image, limit: int, measure: Callable[[int], int]) -> bytes | None:
    """Lower the JPEG quality, then the pixel size, until `measure(encoded)` fits `limit`."""
    current = image
    for _ in range(JPEG_SHRINK_STEPS):
        for quality in JPEG_QUALITIES:
            buffer = io.BytesIO()
            current.save(buffer, format="JPEG", quality=quality, optimize=True)
            encoded = buffer.getvalue()
            if measure(len(encoded)) <= limit:
                return encoded
        if current.width <= JPEG_MIN_SIDE and current.height <= JPEG_MIN_SIDE:
            break
        current = current.resize(
            (max(JPEG_MIN_SIDE, int(current.width * 0.85)), max(JPEG_MIN_SIDE, int(current.height * 0.85))),
            Image.Resampling.LANCZOS,
        )
    return None


def _flatten_for_jpeg(data: bytes) -> Image.Image:
    """Decode any supported source into RGB, compositing transparency on white."""
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            if image.mode in {"RGBA", "LA", "PA"} or "transparency" in image.info:
                rgba = image.convert("RGBA")
                canvas = Image.new("RGB", rgba.size, (255, 255, 255))
                canvas.paste(rgba, mask=rgba.getchannel("A"))
                return canvas
            return image.convert("RGB")
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise StorageError("invalid_image", "图片解码失败") from exc


def _jpeg_name(original_name: str) -> str:
    name = Path(original_name or "").name
    if not name:
        return "upload.jpg"
    return f"{Path(name).stem or 'upload'}.jpg"


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
            animated = image_format in ANIMATED_FORMATS and getattr(source, "n_frames", 1) > 1
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
    # Uploads above the threshold are re-encoded so storage, downloads and AM stay reasonable;
    # the dedupe hash and the reported size still describe the file the user picked.
    stored = compress_large_upload(
        data,
        settings.image_compress_threshold_bytes,
        image_format=image_format,
        original_name=original_name,
        has_alpha=has_alpha,
        animated=animated,
    )
    original_key = object_key(settings, "originals", f"{asset_id}{stored.extension}")
    prepared_key = object_key(settings, "prepared", f"{asset_id}.png")
    written: list[str] = []
    try:
        store.put_bytes(original_key, stored.data, stored.mime_type)
        written.append(original_key)
        store.put_bytes(prepared_key, encode_png(prepared), "image/png")
        written.append(prepared_key)
    except Exception:
        for key in reversed(written):
            try:
                store.delete(key)
            except Exception:  # noqa: BLE001 - preserve the original storage failure
                pass
        raise
    public_url = getattr(store, "public_url", lambda _: None)
    original_url = public_url(original_key)
    prepared_url = public_url(prepared_key)

    return PreparedUpload(
        id=asset_id,
        original_name=original_name or f"upload{stored.extension}",
        path=original_key,
        prepared_path=prepared_key,
        mime_type=stored.mime_type,
        width=prepared.width,
        height=prepared.height,
        bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        url=original_url,
        prepared_url=prepared_url,
        stored_bytes=len(stored.data),
        recompressed=stored.recompressed,
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
