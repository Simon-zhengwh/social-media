"""Read-only SMB source access and validated local-only output.

The source adapter deliberately exposes nothing but enumeration and reads. Every
write goes through :func:`validate_local_output_root`, which rejects UNC paths,
mapped network drives, device paths and reparse-point roots before any directory
is created. Callers never pass a source path to a write helper.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from PIL import Image, ImageOps

from app.services.storage import SUPPORTED_IMAGE_SUFFIXES

MODEL_IMAGE_MAX_SIDE = 1536
MODEL_IMAGE_MAX_BYTES = 1_500_000
PREVIEW_MAX_SIDE = 640
PREVIEW_QUALITY = 82

_ILLEGAL_OUTPUT_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_ILLEGAL_SOURCE_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_DEVICE_PREFIX = re.compile(r"^[\\/]{2}[?.][\\/]")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_DRIVE_REMOVABLE = 2
_DRIVE_FIXED = 3
_DRIVE_REMOTE = 4


class MaterialFilterFileError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class SmbSource:
    server: str
    share: str
    relative: str
    unc: str
    key: str
    display_name: str


@dataclass(frozen=True)
class SourceEntry:
    relative_path: str
    name: str
    size: int
    mtime_ns: int
    absolute_path: str


@dataclass(frozen=True)
class SourceSnapshot:
    relative_path: str
    size: int
    mtime_ns: int
    sha256: str
    data: bytes


def _source_error(message: str, code: str = "source_unavailable") -> MaterialFilterFileError:
    return MaterialFilterFileError(code, message, 422)


def normalize_smb_source(value: str) -> SmbSource:
    """Normalize ``\\\\server\\share\\dir``, ``file://`` and ``smb://`` inputs.

    Only the current Windows session is used: no credentials are accepted and no
    share is mounted. Returns the canonical UNC path plus a case-folded key used
    to keep one TAB per directory.
    """
    raw = str(value or "").strip()
    if not raw:
        raise _source_error("请输入 SMB 文件夹地址", "source_required")
    if raw.startswith("\\\\") or raw.startswith("//"):
        if _DEVICE_PREFIX.match(raw):
            raise _source_error("不支持 Windows 设备路径", "source_device_path")
        server, _, rest = raw[2:].partition("/") if "/" in raw[2:] else raw[2:].partition("\\")
        server = server.strip()
        parts = [part for part in re.split(r"[\\/]+", rest) if part]
        if "@" in server:
            raise _source_error("地址中不能包含账号或密码", "source_credentials_rejected")
    else:
        if re.match(r"^[A-Za-z]:[\\/]", raw):
            raise _source_error(
                "只接受 SMB 共享目录，不接受本机磁盘路径", "source_not_shared"
            )
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        if not scheme:
            raise _source_error(
                "请输入以 \\\\ 开头、file:// 或 smb:// 开头的共享文件夹地址",
                "source_unsupported_form",
            )
        if scheme not in {"file", "smb"}:
            raise _source_error(f"不支持 {scheme}:// 协议，只接受 SMB 共享目录", "source_unsupported_scheme")
        if parsed.query or parsed.fragment:
            raise _source_error("地址不能包含查询参数或片段", "source_invalid")
        if "@" in parsed.netloc:
            raise _source_error("地址中不能包含账号或密码", "source_credentials_rejected")
        server = parsed.netloc.strip()
        if not server:
            # file:///D:/... is a local path, not a connected SMB share.
            raise _source_error("缺少服务器地址", "source_server_required")
        if parsed.port:
            raise _source_error("SMB 地址不能指定端口", "source_invalid")
        parts = [part for part in re.split(r"[\\/]+", unquote(parsed.path)) if part]
        if parts and ":" in parts[0]:
            raise _source_error("只接受 SMB 共享目录，不接受本机磁盘路径", "source_not_shared")

    if not server:
        raise _source_error("缺少服务器地址", "source_server_required")
    if not parts:
        raise _source_error("地址至少需要服务器和共享名，例如 \\\\192.168.11.5\\movies\\照片", "source_share_required")

    share, *tail = parts
    if _ILLEGAL_SOURCE_CHARS.search(server) or _ILLEGAL_SOURCE_CHARS.search(share):
        raise _source_error("地址包含 Windows 不允许的字符", "source_invalid")
    for part in tail:
        if _ILLEGAL_SOURCE_CHARS.search(part):
            raise _source_error("地址包含 Windows 不允许的字符", "source_invalid")
        if part in {".", ".."}:
            raise _source_error("地址不能包含相对路径片段", "source_traversal")
    if share in {".", ".."}:
        raise _source_error("地址不能包含相对路径片段", "source_traversal")

    relative = "\\".join(tail)
    unc = "\\\\" + server + "\\" + "\\".join([share, *tail])
    return SmbSource(
        server=server,
        share=share,
        relative=relative,
        unc=unc,
        key=unc.casefold(),
        display_name=tail[-1] if tail else share,
    )


def check_source_readable(source_root: str | Path) -> None:
    """Enumeration-only access check; never writes a probe file to the share."""
    root = Path(source_root)
    try:
        if not root.is_dir():
            raise _source_error(f"来源目录不存在或不是文件夹：{root}")
        with os.scandir(root) as iterator:
            next(iterator, None)
    except MaterialFilterFileError:
        raise
    except OSError as exc:
        raise _source_error(f"无法读取来源目录：{exc}", "source_unreadable") from exc


def _is_supported(name: str) -> bool:
    return Path(name).suffix.lower() in SUPPORTED_IMAGE_SUFFIXES


def scan_source(
    source_root: str | Path,
    *,
    recursive: bool = False,
    max_files: int = 20000,
) -> list[SourceEntry]:
    root = Path(source_root)
    entries: list[SourceEntry] = []

    def walk(directory: Path, prefix: str) -> None:
        try:
            with os.scandir(directory) as iterator:
                items = sorted(iterator, key=lambda item: item.name.casefold())
        except OSError as exc:
            raise _source_error(f"无法读取来源目录：{exc}", "source_unreadable") from exc
        for item in items:
            if item.name.startswith("."):
                continue
            relative = f"{prefix}/{item.name}" if prefix else item.name
            try:
                if item.is_dir(follow_symlinks=False):
                    if recursive:
                        walk(Path(item.path), relative)
                    continue
                if not item.is_file(follow_symlinks=False) or not _is_supported(item.name):
                    continue
                stat = item.stat(follow_symlinks=False)
            except OSError:
                # A vanished or unreadable entry is skipped rather than failing the scan.
                continue
            entries.append(
                SourceEntry(
                    relative_path=relative,
                    name=item.name,
                    size=int(stat.st_size),
                    mtime_ns=int(stat.st_mtime_ns),
                    absolute_path=str(item.path),
                )
            )
            if len(entries) > max_files:
                raise _source_error(
                    f"来源图片超过单次上限 {max_files} 张，请减少范围或包含子目录的开关",
                    "source_too_large",
                )

    if not root.is_dir():
        raise _source_error(f"来源目录不存在或不是文件夹：{root}")
    walk(root, "")
    return entries


def _read_verified(path: Path, *, max_bytes: int) -> tuple[bytes, int, int]:
    try:
        before = path.stat()
        if before.st_size > max_bytes:
            raise _source_error(
                f"图片超过 {max_bytes // 1_000_000} MB 读取上限：{path.name}",
                "source_too_large",
            )
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
        after = path.stat()
    except MaterialFilterFileError:
        raise
    except OSError as exc:
        raise _source_error(f"无法读取来源图片：{exc}", "source_read_failed") from exc
    if len(data) > max_bytes:
        raise _source_error(f"图片超过读取上限：{path.name}", "source_too_large")
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise _source_error(
            f"图片在读取过程中发生变化，请稍后重试：{path.name}",
            "source_changed",
        )
    return data, int(after.st_size), int(after.st_mtime_ns)


def read_source_snapshot(
    source_root: str | Path,
    entry: SourceEntry,
    *,
    max_bytes: int = 40_000_000,
) -> SourceSnapshot:
    path = Path(entry.absolute_path)
    data, size, mtime_ns = _read_verified(path, max_bytes=max_bytes)
    return SourceSnapshot(
        relative_path=entry.relative_path,
        size=size,
        mtime_ns=mtime_ns,
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


def read_source_file(
    source_root: str | Path,
    relative_path: str,
    *,
    max_bytes: int = 40_000_000,
) -> SourceSnapshot:
    path = Path(source_root) / relative_path
    data, size, mtime_ns = _read_verified(path, max_bytes=max_bytes)
    return SourceSnapshot(
        relative_path=relative_path,
        size=size,
        mtime_ns=mtime_ns,
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_preview_jpeg(data: bytes, *, max_side: int = PREVIEW_MAX_SIDE) -> bytes:
    try:
        with Image.open(io.BytesIO(data)) as opened:
            image = ImageOps.exif_transpose(opened)
            image.load()
            image = image.convert("RGB")
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=PREVIEW_QUALITY, optimize=True)
            return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001 - untrusted source image
        raise _source_error(f"图片无法解码：{exc}", "source_invalid_image") from exc


def prepare_model_image_data_url(
    data: bytes,
    *,
    max_side: int = MODEL_IMAGE_MAX_SIDE,
    max_bytes: int = MODEL_IMAGE_MAX_BYTES,
) -> str:
    """Downscaled, EXIF-corrected data URL; the provider never receives the source path."""
    import base64

    try:
        with Image.open(io.BytesIO(data)) as opened:
            image = ImageOps.exif_transpose(opened)
            image.load()
            image = image.convert("RGB")
    except Exception as exc:  # noqa: BLE001 - untrusted source image
        raise _source_error(f"图片无法解码：{exc}", "source_invalid_image") from exc
    current = image
    current.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    encoded = b""
    for _ in range(6):
        for quality in (88, 82, 76, 70, 64):
            buffer = io.BytesIO()
            current.save(buffer, format="JPEG", quality=quality, optimize=True)
            encoded = buffer.getvalue()
            if len(encoded) <= max_bytes:
                return "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")
        current.thumbnail(
            (max(256, int(current.width * 0.85)), max(256, int(current.height * 0.85))),
            Image.Resampling.LANCZOS,
        )
    return "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")


def sanitize_output_stem(value: str, *, fallback: str = "图片", max_length: int = 40) -> str:
    cleaned = _ILLEGAL_OUTPUT_CHARS.sub("", str(value or "")).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        cleaned = fallback
    return cleaned[:max_length].strip(" .") or fallback


def output_filename(display_name: str, short_id: str, suffix: str) -> str:
    suffix = suffix if suffix.startswith(".") else f".{suffix}" if suffix else ".jpg"
    return f"{sanitize_output_stem(display_name)}__{short_id}{suffix.lower()}"


def _default_drive_type(path: str) -> int:
    if os.name != "nt":
        return _DRIVE_FIXED
    drive = os.path.splitdrive(path)[0]
    if not drive:
        return 0
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetDriveTypeW(drive + "\\"))
    except Exception:  # noqa: BLE001 - unknown drive type is rejected by the caller
        return 0


def _default_final_path(path: Path) -> Path:
    return Path(os.path.realpath(path))


def _is_reparse_point(path: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        stat = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    attributes = getattr(stat, "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def validate_local_output_root(
    root: str | Path,
    *,
    drive_type=_default_drive_type,
    final_path=_default_final_path,
) -> Path:
    """Return the resolved local-only directory, or raise before any write happens."""
    raw = str(root or "").strip()
    if not raw:
        raise MaterialFilterFileError("output_required", "请配置本机输出目录", 422)
    if _DEVICE_PREFIX.match(raw):
        raise MaterialFilterFileError("output_device_path", "输出目录不能是 Windows 设备路径", 422)
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise MaterialFilterFileError(
            "output_network_path_rejected",
            "输出目录必须在本地固定磁盘，不能使用 SMB/UNC 路径",
            422,
        )
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise MaterialFilterFileError("output_not_absolute", "输出目录必须是绝对路径", 422)
    resolved = Path(final_path(candidate))
    if not resolved.is_absolute():
        raise MaterialFilterFileError("output_not_absolute", "输出目录必须是绝对路径", 422)
    if ".." in resolved.parts:
        raise MaterialFilterFileError("output_traversal", "输出目录不能包含相对路径片段", 422)
    if str(resolved).startswith("\\\\") or str(resolved).startswith("//"):
        raise MaterialFilterFileError(
            "output_network_path_rejected",
            "输出目录解析后仍指向网络路径，已拒绝",
            422,
        )
    drive = drive_type(str(resolved))
    if drive == _DRIVE_REMOTE:
        raise MaterialFilterFileError(
            "output_network_drive_rejected",
            "输出目录位于映射网络盘，必须改用本地固定磁盘",
            422,
        )
    if drive not in {_DRIVE_FIXED, _DRIVE_REMOVABLE}:
        raise MaterialFilterFileError(
            "output_drive_rejected",
            "输出目录不在本地固定磁盘上，已拒绝",
            422,
        )
    probe = resolved
    while True:
        if probe.exists():
            if _is_reparse_point(probe):
                raise MaterialFilterFileError(
                    "output_reparse_rejected",
                    "输出目录包含目录链接或 junction，必须改用普通本机目录",
                    422,
                )
            break
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    return resolved


def ensure_local_directory(
    root: str | Path,
    *,
    drive_type=_default_drive_type,
    final_path=_default_final_path,
) -> Path:
    resolved = validate_local_output_root(root, drive_type=drive_type, final_path=final_path)
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MaterialFilterFileError(
            "output_unwritable", f"无法创建本机输出目录：{exc}", 422
        ) from exc
    # A junction could still be swapped in between validation and creation.
    return validate_local_output_root(resolved, drive_type=drive_type, final_path=final_path)


def _atomic_write(directory: Path, file_name: str, data: bytes) -> Path:
    directory = ensure_local_directory(directory)
    target = directory / file_name
    if not target.resolve().is_relative_to(directory):
        raise MaterialFilterFileError("output_traversal", "输出文件名越界", 422)
    temp = directory / f".{uuid.uuid4().hex}.tmp"
    try:
        with temp.open("xb") as handle:
            handle.write(data)
        os.replace(temp, target)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass
    return target


def write_local_preview(directory: str | Path, evaluation_id: str, data: bytes) -> str:
    preview_dir = ensure_local_directory(Path(directory) / "previews")
    jpeg = build_preview_jpeg(data)
    name = f"{sanitize_output_stem(evaluation_id, fallback='preview', max_length=60)}.jpg"
    _atomic_write(preview_dir, name, jpeg)
    return f"previews/{name}"


def copy_approved_bytes(
    directory: str | Path,
    *,
    display_name: str,
    short_id: str,
    suffix: str,
    data: bytes,
    sha256: str,
) -> tuple[str, str]:
    """Copy the untouched source bytes to the local passed directory.

    Returns ``(file_name, sha256)``. A file with the same name and different bytes
    is never overwritten.
    """
    passed_dir = ensure_local_directory(Path(directory) / "passed")
    file_name = output_filename(display_name, short_id, suffix)
    target = passed_dir / file_name
    if target.exists():
        existing = hashlib.sha256(target.read_bytes()).hexdigest()
        if existing == sha256:
            return file_name, existing
        raise MaterialFilterFileError(
            "output_conflict",
            f"目标副本已存在且内容不同，拒绝覆盖：{file_name}",
            409,
        )
    _atomic_write(passed_dir, file_name, data)
    written = hashlib.sha256(target.read_bytes()).hexdigest()
    if written != sha256:
        raise MaterialFilterFileError("output_hash_mismatch", "副本校验失败，已隔离为不可用", 500)
    return file_name, written
