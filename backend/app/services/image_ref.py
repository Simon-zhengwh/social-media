from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ImageRef:
    """A model image input backed by a local file or a public URL."""

    path: Path | None = None
    url: str | None = None
    name: str = ""
    sha256: str = ""
    owned: bool = False

    def cleanup(self) -> None:
        if self.owned and self.path is not None:
            self.path.unlink(missing_ok=True)


def image_ref_from_path(path: Path, *, owned: bool = False, name: str | None = None) -> ImageRef:
    resolved = path.resolve()
    return ImageRef(path=resolved, name=name or resolved.name, owned=owned)


def image_ref_name(value: ImageRef | Path) -> str:
    if isinstance(value, ImageRef):
        if value.name:
            return value.name
        if value.path is not None:
            return value.path.name
        return "reference"
    return Path(value).name


def image_ref_hash(value: ImageRef | Path) -> str:
    if isinstance(value, ImageRef):
        if value.sha256:
            return value.sha256
        if value.path is not None:
            try:
                return hashlib.sha256(value.path.read_bytes()).hexdigest()
            except OSError:
                return value.name or "reference"
        return value.url or value.name or "reference"
    try:
        return hashlib.sha256(Path(value).read_bytes()).hexdigest()
    except OSError:
        return Path(value).name


def _is_public_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def image_ref_from_asset(
    db: Any,
    store: Any,
    asset_id: str,
    *,
    suffix: str = ".png",
    expires: int = 3600,
) -> ImageRef:
    asset = db.assets.find_one({"_id": asset_id})
    if asset is None:
        raise FileNotFoundError(f"素材不存在：{asset_id}")
    relative = asset.get("prepared_path") or asset.get("path")
    if not relative:
        raise FileNotFoundError(f"素材缺少对象路径：{asset_id}")
    name = str(asset.get("original_name") or Path(relative).name)
    digest = str(asset.get("sha256") or "")
    local_path = store.local_path(relative)
    if local_path is not None:
        return ImageRef(path=local_path, name=name, sha256=digest)
    try:
        url = store.presign(relative, expires=expires)
        if _is_public_url(url):
            return ImageRef(url=url, name=name, sha256=digest)
    except Exception:  # noqa: BLE001 - fall back to a downloaded local file
        pass
    path = store.download_to_temp(relative, suffix=suffix)
    return ImageRef(path=path, name=name, sha256=digest, owned=True)


def cleanup_image_refs(references: list[ImageRef] | tuple[ImageRef, ...]) -> None:
    for reference in references:
        reference.cleanup()
