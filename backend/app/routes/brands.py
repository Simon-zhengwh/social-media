from __future__ import annotations

import mimetypes
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.services import brand_style

router = APIRouter(prefix="/api/brands", tags=["brands"])


@router.get("")
def list_brands(request: Request) -> list[dict[str, Any]]:
    settings = request.app.state.settings
    return [
        brand.public_dict(cover_url=f"/api/brands/{brand.id}/cover")
        for brand in brand_style.list_brands(settings.skills_path)
    ]


@router.get("/{brand_id}/cover")
def brand_cover(brand_id: str, request: Request) -> FileResponse:
    settings = request.app.state.settings
    try:
        brand = brand_style.get_brand(settings.skills_path, brand_id)
    except brand_style.BrandError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    if brand.cover_path is None or not brand.cover_path.is_file():
        raise HTTPException(status_code=404, detail="品牌封面不存在")
    media_type = mimetypes.guess_type(brand.cover_path.name)[0] or "image/jpeg"
    return FileResponse(
        brand.cover_path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )
