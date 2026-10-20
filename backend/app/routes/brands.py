from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile
from PIL import Image, ImageOps

from app.schemas import BrandUpdate
from app.services import brand_style, storage

router = APIRouter(prefix="/api/brands", tags=["brands"])


def _asset_document(prepared: storage.PreparedUpload) -> dict[str, Any]:
    return {
        "_id": prepared.id,
        "kind": prepared.kind,
        "original_name": prepared.original_name,
        "path": prepared.path,
        "prepared_path": prepared.prepared_path,
        "mime_type": prepared.mime_type,
        "width": prepared.width,
        "height": prepared.height,
        "bytes": prepared.bytes,
        "sha256": prepared.sha256,
        "created_at": datetime.now(timezone.utc),
    }


def _brand_document(db: Any, brand_id: str) -> dict[str, Any]:
    document = db.brands.find_one({"_id": brand_id})
    if document is None:
        raise HTTPException(status_code=404, detail="品牌风格不存在")
    return document


def _brand_detail(db: Any, document: dict[str, Any]) -> dict[str, Any]:
    brand = brand_style.brand_from_document(document)
    reference_ids = list(brand.reference_asset_ids)
    cover_asset_id = brand.cover_asset_id or (reference_ids[0] if reference_ids else None)
    references: list[dict[str, Any]] = []
    for asset_id in reference_ids:
        asset = db.assets.find_one({"_id": asset_id})
        if asset is None:
            continue
        references.append({
            "asset_id": asset_id,
            "original_name": asset.get("original_name") or "",
            "url": f"/api/assets/{asset_id}/file",
            "width": int(asset.get("width") or 0),
            "height": int(asset.get("height") or 0),
            "is_cover": asset_id == cover_asset_id,
        })
    return {
        "id": brand.id,
        "name": brand.name,
        "short_name": brand.short_name,
        "summary": brand.summary,
        "is_default": brand.is_default,
        "cover_url": f"/api/brands/{brand.id}/cover" if cover_asset_id else None,
        "cover_asset_id": cover_asset_id,
        "layout_guide": brand.layout_guide,
        "background_guide": brand.background_guide,
        "logo_keywords": list(brand.logo_keywords),
        "reference_images": references,
    }


@router.get("")
def list_brands(request: Request) -> list[dict[str, Any]]:
    db = request.app.state.db
    return [
        brand.public_dict(
            cover_url=(
                f"/api/brands/{brand.id}/cover"
                if brand.cover_asset_id or brand.reference_asset_ids
                else None
            )
        )
        for brand in brand_style.list_brands(db)
    ]


@router.get("/{brand_id}")
def get_brand(brand_id: str, request: Request) -> dict[str, Any]:
    try:
        return _brand_detail(request.app.state.db, _brand_document(request.app.state.db, brand_id))
    except brand_style.BrandError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc


@router.put("/{brand_id}")
def update_brand(
    brand_id: str,
    payload: BrandUpdate,
    request: Request,
) -> dict[str, Any]:
    db = request.app.state.db
    document = _brand_document(db, brand_id)
    reference_ids = list(document.get("reference_asset_ids") or [])[:3]
    if payload.cover_asset_id and payload.cover_asset_id not in reference_ids:
        raise HTTPException(status_code=422, detail="封面必须来自当前品牌的参考图")
    if payload.is_default:
        db.brands.update_many(
            {"_id": {"$ne": brand_id}},
            {"$set": {"is_default": False}},
        )
    cover_asset_id = payload.cover_asset_id or (reference_ids[0] if reference_ids else None)
    db.brands.update_one(
        {"_id": brand_id},
        {
            "$set": {
                "name": payload.name.strip(),
                "short_name": payload.short_name.strip() or payload.name.strip(),
                "summary": payload.summary.strip(),
                "layout_guide": payload.layout_guide.strip(),
                "background_guide": payload.background_guide.strip(),
                "logo_keywords": payload.logo_keywords,
                "is_default": payload.is_default,
                "cover_asset_id": cover_asset_id,
                "updated_at": datetime.now(timezone.utc),
            },
        },
    )
    return _brand_detail(db, _brand_document(db, brand_id))


@router.post("/{brand_id}/references")
def upload_reference(
    brand_id: str,
    request: Request,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    db = request.app.state.db
    settings = request.app.state.settings
    store = request.app.state.store
    document = _brand_document(db, brand_id)
    reference_ids = list(document.get("reference_asset_ids") or [])[:3]
    if len(reference_ids) >= 3:
        raise HTTPException(status_code=422, detail="品牌参考图最多 3 张")
    data = file.file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="文件超过 10 MB 限制")
    digest = hashlib.sha256(data).hexdigest()
    asset = db.assets.find_one({"kind": "brand_reference", "sha256": digest})
    if asset is None:
        prepared = storage.prepare_upload(
            settings,
            store,
            data,
            file.filename or "",
            "brand_reference",
        )
        asset = _asset_document(prepared)
        db.assets.insert_one(asset)
    asset_id = str(asset["_id"])
    if asset_id not in reference_ids:
        reference_ids.append(asset_id)
    cover_asset_id = document.get("cover_asset_id") or reference_ids[0]
    db.brands.update_one(
        {"_id": brand_id},
        {
            "$set": {
                "reference_asset_ids": reference_ids,
                "cover_asset_id": cover_asset_id,
                "updated_at": datetime.now(timezone.utc),
            },
        },
    )
    return _brand_detail(db, _brand_document(db, brand_id))


@router.delete("/{brand_id}/references/{asset_id}")
def delete_reference(brand_id: str, asset_id: str, request: Request) -> dict[str, Any]:
    db = request.app.state.db
    document = _brand_document(db, brand_id)
    reference_ids = list(document.get("reference_asset_ids") or [])
    if asset_id not in reference_ids:
        raise HTTPException(status_code=404, detail="参考图不存在")
    remaining = [item for item in reference_ids if item != asset_id]
    cover_asset_id = document.get("cover_asset_id")
    if cover_asset_id == asset_id:
        cover_asset_id = remaining[0] if remaining else None
    db.brands.update_one(
        {"_id": brand_id},
        {
            "$set": {
                "reference_asset_ids": remaining,
                "cover_asset_id": cover_asset_id,
                "updated_at": datetime.now(timezone.utc),
            },
        },
    )
    return _brand_detail(db, _brand_document(db, brand_id))


@router.get("/{brand_id}/cover")
def brand_cover(brand_id: str, request: Request) -> Response:
    db = request.app.state.db
    store = request.app.state.store
    try:
        brand = brand_style.get_brand(db, brand_id)
    except brand_style.BrandError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc

    asset_id = brand.cover_asset_id
    if not asset_id and brand.reference_asset_ids:
        asset_id = brand.reference_asset_ids[0]
    if not asset_id:
        raise HTTPException(status_code=404, detail="品牌封面不存在")

    asset = db.assets.find_one({"_id": asset_id})
    if asset is None:
        raise HTTPException(status_code=404, detail="品牌封面不存在")
    relative = asset.get("prepared_path") or asset.get("path")
    if not relative:
        raise HTTPException(status_code=404, detail="品牌封面不存在")
    try:
        data = store.get_bytes(relative)
    except Exception as exc:  # noqa: BLE001 - missing object is a 404 for cover
        raise HTTPException(status_code=404, detail="品牌封面不存在") from exc

    with Image.open(BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((640, 640), Image.Resampling.LANCZOS)
        output = BytesIO()
        image.save(output, format="JPEG", quality=86, optimize=True)
    return Response(
        content=output.getvalue(),
        media_type="image/jpeg",
        headers={"Cache-Control": "no-cache"},
    )
