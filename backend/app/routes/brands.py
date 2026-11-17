from __future__ import annotations

import uuid
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

import httpx
from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile
from PIL import Image, ImageOps

from app.api_paths import asset_url, brand_cover_url
from app.request_context import request_db, request_merchant_id, request_profile, request_store
from app.schemas import BrandUpdate, DuplicateBrandRequest
from app.services import (
    asset_records,
    brand_rules,
    brand_seed,
    brand_style,
    merchant_logos,
    merchant_profile,
    platform_presets,
)

router = APIRouter(prefix="/brands", tags=["brands"])

AM_HTTP = httpx.Client(timeout=30, follow_redirects=True)
MAX_SEED_LOGO_BYTES = 20_000_000


def _brand_document(db: Any, brand_id: str) -> dict[str, Any]:
    document = db.brands.find_one({"_id": brand_id})
    if document is None:
        raise HTTPException(status_code=404, detail="品牌风格不存在")
    return document


def _reference_images(db: Any, merchant_id: str, brand: brand_style.BrandStyle) -> list[dict[str, Any]]:
    """Every reference of a style, in the order the operator arranged them.

    The list endpoint and the detail endpoint share this shape, because the style picker shows
    all references and the detail page manages them.
    """
    reference_ids = list(brand.reference_asset_ids)[: brand_style.MAX_BRAND_REFERENCES]
    cover_asset_id = brand.cover_asset_id or (reference_ids[0] if reference_ids else None)
    images: list[dict[str, Any]] = []
    for asset_id in reference_ids:
        asset = db.assets.find_one({"_id": asset_id})
        if asset is None:
            continue
        images.append({
            "asset_id": asset_id,
            "original_name": asset.get("original_name") or "",
            "url": asset.get("prepared_url") or asset.get("url") or asset_url(merchant_id, asset_id),
            "width": int(asset.get("width") or 0),
            "height": int(asset.get("height") or 0),
            "is_cover": asset_id == cover_asset_id,
        })
    return images


def _brand_detail(request: Request, db: Any, document: dict[str, Any]) -> dict[str, Any]:
    merchant_id = str(document.get("merchant_id") or request_merchant_id(request))
    brand = brand_style.brand_from_document(document)
    reference_ids = list(brand.reference_asset_ids)
    cover_asset_id = brand.cover_asset_id or (reference_ids[0] if reference_ids else None)
    references = _reference_images(db, merchant_id, brand)
    logo_images = merchant_logos.images(db, merchant_id)
    return {
        "id": brand.id,
        "merchant_profile": (request_profile(request) if getattr(getattr(request, "state", None), "merchant_id", None) else merchant_profile.get_profile(db, brand.merchant_id)),
        "name": brand.name,
        "short_name": brand.short_name,
        "summary": brand.summary,
        "schema_version": brand.schema_version,
        "style_fields": brand.style_fields,
        "legacy_guides": brand.legacy_guides,
        "platform": brand.platform,
        "platform_id": brand.platform_id,
        "placement_id": brand.placement_id,
        "purpose": brand.purpose,
        "note": brand.note,
        "is_default": brand.is_default,
        "cover_url": brand_cover_url(merchant_id, brand.id) if cover_asset_id else None,
        "cover_asset_id": cover_asset_id,
        "layout_guide": brand.layout_guide,
        "background_guide": brand.background_guide,
        "logo_keywords": list(brand.logo_keywords),
        "colors": [dict(item) for item in brand.colors],
        "logo_asset_ids": [item["asset_id"] for item in logo_images],
        "logo_images": logo_images,
        "reference_images": references,
    }


def _seed_profile(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    """Reduce the projected AM profile to the shape brand_seed reads."""
    if not profile:
        return None
    brand = profile.get("raw_brand")
    return {
        "merchant_id": profile.get("source_record_id") or profile.get("id") or "",
        "merchant_name": profile.get("name") or "",
        "brand": brand if isinstance(brand, dict) else {},
    }


def _fetch_remote_image(url: str) -> bytes:
    """Download a merchant logo for seeding; bounded so a bad URL cannot fill memory.

    Failures are raised, not swallowed here: the seed keeps the style and only drops the logo,
    so a broken link never costs the operator the derived fields.
    """
    try:
        response = AM_HTTP.get(url, follow_redirects=True)
    except httpx.RequestError as exc:
        raise ValueError(f"下载商户 Logo 失败：{exc}") from exc
    if response.status_code != 200:
        raise ValueError(f"下载商户 Logo 失败（HTTP {response.status_code}）")
    data = response.content
    if len(data) > MAX_SEED_LOGO_BYTES:
        raise ValueError("商户 Logo 超过允许导入的大小")
    return data


@router.get("")
def list_brands(request: Request) -> list[dict[str, Any]]:
    db = request_db(request)
    merchant_id = request_merchant_id(request)
    # Every style shares the merchant's logos, so the list can answer without a per-brand read.
    logo_images = merchant_logos.images(db, merchant_id)
    items: list[dict[str, Any]] = []
    for brand in brand_style.list_brands(db):
        item = brand.public_dict(
            cover_url=(
                brand_cover_url(merchant_id, brand.id)
                if brand.cover_asset_id or brand.reference_asset_ids
                else None
            )
        )
        item["logo_images"] = [dict(image) for image in logo_images]
        item["logo_asset_ids"] = [image["asset_id"] for image in logo_images]
        # The style picker previews every reference, not just the cover, so the list carries them.
        item["reference_images"] = _reference_images(db, merchant_id, brand)
        items.append(item)
    return items


@router.post("/preview")
def preview_brand(payload: BrandUpdate, request: Request) -> dict[str, str]:
    try:
        return {"prompt": brand_rules.compile_brand_preview(request_db(request), payload.model_dump())}
    except platform_presets.PlatformError as exc:
        raise HTTPException(422, exc.message) from exc


@router.post("", status_code=201)
def create_brand(payload: BrandUpdate, request: Request) -> dict[str, Any]:
    db = request_db(request)
    brand_id = f"brand-{uuid.uuid4().hex}"
    # Reuse update validation; remove the provisional record if validation fails.
    if payload.cover_asset_id:
        raise HTTPException(422, "请先保存风格，再上传参考图设置封面")
    now = datetime.now(timezone.utc)
    document = {"_id": brand_id, "created_at": now, "reference_asset_ids": []}
    db.brands.insert_one(document)
    try:
        return update_brand(brand_id, payload, request)
    except Exception:
        db.brands.delete_one({"_id": brand_id})
        raise


@router.post("/initialize-from-profile", status_code=201)
def initialize_from_profile(request: Request) -> dict[str, Any]:
    """Create the merchant's first style from its AM brand profile.

    The profile is already loaded by the merchant middleware for this request; when it is
    missing the merchant has no AM brand record and there is nothing to derive a style from.
    """
    db = request_db(request)
    merchant_id = request_merchant_id(request)
    try:
        document, created = brand_seed.initialize(
            db,
            _seed_profile(request_profile(request)),
            settings=request.app.state.settings,
            store=request_store(request),
            merchant_id=merchant_id,
            fetch_image=_fetch_remote_image,
        )
    except brand_seed.SeedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return {**_brand_detail(request, db, document), "created": created}


@router.get("/{brand_id}")
def get_brand(brand_id: str, request: Request) -> dict[str, Any]:
    try:
        db = request_db(request)
        return _brand_detail(request, db, _brand_document(db, brand_id))
    except brand_style.BrandError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc


@router.put("/{brand_id}")
def update_brand(
    brand_id: str,
    payload: BrandUpdate,
    request: Request,
) -> dict[str, Any]:
    db = request_db(request)
    document = _brand_document(db, brand_id)
    reference_ids = list(document.get("reference_asset_ids") or [])[:brand_style.MAX_BRAND_REFERENCES]
    if payload.cover_asset_id and payload.cover_asset_id not in reference_ids:
        raise HTTPException(status_code=422, detail="封面必须来自当前品牌的参考图")
    requested_platform = (
        payload.platform_id
        if "platform_id" in payload.model_fields_set
        else payload.platform
        or str(document.get("platform_id") or document.get("platform") or "")
    )
    requested_placement = (
        payload.placement_id
        if "placement_id" in payload.model_fields_set
        else str(document.get("placement_id") or "")
    )
    try:
        platform_id, placement_id, purpose = brand_style.platform_placement(
            requested_platform, requested_placement
        )
    except brand_style.BrandError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc
    display_platform = brand_style.platform_name(platform_id)

    colors = (
        [item.model_dump() for item in payload.colors]
        if "colors" in payload.model_fields_set
        else brand_style._normalize_colors(document.get("colors"))
    )

    style_fields = payload.style_fields.model_dump() if payload.style_fields is not None else document.get("style_fields")
    layout_guide = brand_rules.compile_style_fields(style_fields) if style_fields else payload.layout_guide.strip()
    background_guide = layout_guide if style_fields else payload.background_guide.strip()
    # Saving notes must preserve the stored guides when hidden fields are unchanged.
    if style_fields and style_fields == document.get("style_fields"):
        layout_guide = document.get("layout_guide", layout_guide)
        background_guide = document.get("background_guide", background_guide)
    template_updates: dict[str, Any] = {}
    if payload.schema_version == 2 or style_fields is not None:
        template_updates.update(schema_version=2, style_fields=style_fields)
        if style_fields is not None and document.get("schema_version") != 2 and not document.get("legacy_guides") and document.get("layout_guide"):
            template_updates["legacy_guides"] = {"layout_guide": document.get("layout_guide", ""), "background_guide": document.get("background_guide", "")}

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
                **template_updates,
                "name": payload.name.strip(),
                "short_name": payload.short_name.strip() or payload.name.strip(),
                "summary": payload.summary.strip(),
                "platform": display_platform,
                "platform_id": platform_id,
                "placement_id": placement_id,
                "purpose": purpose,
                "note": payload.note.strip(),
                "colors": list(colors),
                # Legacy field: logos live on the merchant now, so saving a style must not touch them.
                "layout_guide": layout_guide,
                "background_guide": background_guide,
                "logo_keywords": payload.logo_keywords,
                "is_default": payload.is_default,
                "cover_asset_id": cover_asset_id,
                "updated_at": datetime.now(timezone.utc),
            },
        },
    )
    return _brand_detail(request, db, _brand_document(db, brand_id))


@router.delete("/{brand_id}", status_code=204)
def delete_brand(brand_id: str, request: Request) -> Response:
    db = request_db(request)
    document = _brand_document(db, brand_id)
    was_default = bool(document.get("is_default"))
    db.brands.delete_one({"_id": brand_id})

    if was_default:
        remaining = next(
            iter(
                db.brands.find({}).sort([("created_at", 1), ("_id", 1)]).limit(1)
            ),
            None,
        )
        if remaining is not None:
            db.brands.update_one(
                {"_id": remaining["_id"]},
                {"$set": {"is_default": True, "updated_at": datetime.now(timezone.utc)}},
            )
    return Response(status_code=204)

@router.post("/{brand_id}/duplicate", status_code=201)
def duplicate_brand(
    brand_id: str,
    payload: DuplicateBrandRequest,
    request: Request,
) -> dict[str, Any]:
    db = request_db(request)
    source = _brand_document(db, brand_id)
    requested_platform = payload.platform_id or str(
        source.get("platform_id") or source.get("platform") or ""
    )
    requested_placement = payload.placement_id or str(source.get("placement_id") or "")
    try:
        platform_id, placement_id, purpose = brand_style.platform_placement(
            requested_platform, requested_placement
        )
    except brand_style.BrandError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc

    duplicate_id = f"{brand_id[:55]}-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    document = dict(source)
    document.pop("logo_asset_ids", None)
    document.update({
        "_id": duplicate_id,
        "name": payload.name or f"{source.get('name') or brand_id} 副本",
        "is_default": False,
        "platform": brand_style.platform_name(platform_id),
        "platform_id": platform_id,
        "placement_id": placement_id,
        "purpose": purpose,
        "created_at": now,
        "updated_at": now,
    })
    db.brands.insert_one(document)
    return _brand_detail(request, db, document)


def _persist_uploaded_asset(
    db: Any,
    settings: Any,
    store: Any,
    file: UploadFile,
) -> dict[str, Any]:
    data = file.file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过 {settings.max_upload_bytes / 1_000_000:g} MB 限制",
        )
    asset, _ = asset_records.persist_upload(
        db, settings, store, data, file.filename or ""
    )
    return asset


@router.post("/{brand_id}/references")
def upload_reference(
    brand_id: str,
    request: Request,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    db = request_db(request)
    settings = request.app.state.settings
    store = request_store(request)
    document = _brand_document(db, brand_id)
    reference_ids = list(document.get("reference_asset_ids") or [])[:brand_style.MAX_BRAND_REFERENCES]
    if len(reference_ids) >= brand_style.MAX_BRAND_REFERENCES:
        raise HTTPException(status_code=422, detail=f"品牌参考图最多 {brand_style.MAX_BRAND_REFERENCES} 张")
    asset = _persist_uploaded_asset(db, settings, store, file)
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
    return _brand_detail(request, db, _brand_document(db, brand_id))


@router.delete("/{brand_id}/references/{asset_id}")
def delete_reference(brand_id: str, asset_id: str, request: Request) -> dict[str, Any]:
    db = request_db(request)
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
    return _brand_detail(request, db, _brand_document(db, brand_id))


@router.get("/{brand_id}/cover")
def brand_cover(brand_id: str, request: Request) -> Response:
    db = request_db(request)
    store = request_store(request)
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
