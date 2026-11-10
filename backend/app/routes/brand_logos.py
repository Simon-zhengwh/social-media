from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from app.request_context import request_db, request_merchant_id, request_store
from app.services import asset_records, merchant_logos

router = APIRouter(prefix="/brand-logos", tags=["brand-logos"])


def _payload(db: Any, merchant_id: str) -> dict[str, Any]:
    logo_images = merchant_logos.images(db, merchant_id)
    return {
        "logo_asset_ids": [item["asset_id"] for item in logo_images],
        "logo_images": logo_images,
    }


@router.get("")
def list_brand_logos(request: Request) -> dict[str, Any]:
    return _payload(request_db(request), request_merchant_id(request))


@router.post("")
def upload_brand_logos(
    request: Request,
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    """Append logos to the merchant-level set; new uploads always land in the asset library."""
    db = request_db(request)
    settings = request.app.state.settings
    store = request_store(request)
    logo_ids = merchant_logos.get_asset_ids(db)
    remaining = merchant_logos.MAX_MERCHANT_LOGOS - len(logo_ids)
    if remaining <= 0:
        raise HTTPException(
            status_code=422,
            detail=f"品牌 Logo 最多 {merchant_logos.MAX_MERCHANT_LOGOS} 张",
        )
    for file in files[:remaining]:
        data = file.file.read(settings.max_upload_bytes + 1)
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"文件超过 {settings.max_upload_bytes / 1_000_000:g} MB 限制",
            )
        asset, _ = asset_records.persist_upload(db, settings, store, data, file.filename or "")
        asset_id = str(asset["_id"])
        if asset_id not in logo_ids:
            logo_ids.append(asset_id)
    merchant_logos.set_asset_ids(db, logo_ids)
    return _payload(db, request_merchant_id(request))


@router.delete("/{asset_id}")
def delete_brand_logo(asset_id: str, request: Request) -> dict[str, Any]:
    db = request_db(request)
    logo_ids = merchant_logos.get_asset_ids(db)
    if asset_id not in logo_ids:
        raise HTTPException(status_code=404, detail="品牌 Logo 不存在")
    merchant_logos.set_asset_ids(db, [item for item in logo_ids if item != asset_id])
    return _payload(db, request_merchant_id(request))
