from __future__ import annotations

from urllib.parse import quote


def merchant_base(merchant_id: str) -> str:
    return f"/api/merchants/{quote(str(merchant_id), safe='')}"


def asset_url(merchant_id: str, asset_id: str, *, version: int | None = None) -> str:
    suffix = f"?v={version}" if version is not None else ""
    if not merchant_id:
        return f"/api/assets/{quote(str(asset_id), safe='')}/file{suffix}"
    return f"{merchant_base(merchant_id)}/assets/{quote(str(asset_id), safe='')}/file{suffix}"


def brand_cover_url(merchant_id: str, brand_id: str) -> str:
    if not merchant_id:
        return f"/api/brands/{quote(str(brand_id), safe='')}/cover"
    return f"{merchant_base(merchant_id)}/brands/{quote(str(brand_id), safe='')}/cover"


def whole_download_url(merchant_id: str, task_id: str, version_id: str, output_id: str | None = None) -> str:
    base = f"{merchant_base(merchant_id) if merchant_id else '/api'}/whole-image/tasks/{quote(str(task_id), safe='')}/download"
    query = f"version_id={quote(str(version_id), safe='')}"
    if output_id:
        query += f"&output_id={quote(str(output_id), safe='')}"
    return f"{base}?{query}"


def poster_download_url(merchant_id: str, task_id: str, output_id: str) -> str:
    if not merchant_id:
        return f"/api/poster-edits/{quote(str(task_id), safe='')}/download?output_id={quote(str(output_id), safe='')}"
    return (
        f"{merchant_base(merchant_id)}/poster-edits/{quote(str(task_id), safe='')}/download"
        f"?output_id={quote(str(output_id), safe='')}"
    )


def background_download_url(merchant_id: str, task_id: str) -> str:
    if not merchant_id:
        return f"/api/background-replacements/{quote(str(task_id), safe='')}/download"
    return f"{merchant_base(merchant_id)}/background-replacements/{quote(str(task_id), safe='')}/download"
