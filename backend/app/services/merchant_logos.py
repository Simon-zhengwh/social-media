from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.api_paths import asset_url
from app.services.scoped_db import ScopedCollection

# The brand logo belongs to the merchant, not to one platform style: every style shares it.
MAX_MERCHANT_LOGOS = 5


def _normalized(ids: list[Any]) -> list[str]:
    return [str(item) for item in dict.fromkeys(ids) if str(item)][:MAX_MERCHANT_LOGOS]


def _merchant_id(db: Any) -> str | None:
    collection = db.merchant_logos
    return collection.merchant_id if isinstance(collection, ScopedCollection) else None


def _document_id(db: Any) -> str:
    return _merchant_id(db) or "default"


def _legacy_asset_ids(db: Any) -> list[str]:
    """Read the pre-migration logos off the merchant's styles, default style first."""
    ids: list[str] = []
    for document in db.brands.find({}).sort([("is_default", -1), ("created_at", 1), ("_id", 1)]):
        for asset_id in document.get("logo_asset_ids") or []:
            value = str(asset_id)
            if value and value not in ids:
                ids.append(value)
            if len(ids) >= MAX_MERCHANT_LOGOS:
                return ids
    return ids


def get_asset_ids(db: Any) -> list[str]:
    """The merchant's logos, falling back read-only to legacy per-style logos."""
    document = db.merchant_logos.find_one({"_id": _document_id(db)})
    if document is not None:
        return _normalized(document.get("logo_asset_ids") or [])
    return _legacy_asset_ids(db)


def set_asset_ids(db: Any, asset_ids: list[str]) -> list[str]:
    values = _normalized(asset_ids)
    db.merchant_logos.update_one(
        {"_id": _document_id(db)},
        {"$set": {"logo_asset_ids": values, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return values


def has_record(db: Any) -> bool:
    return db.merchant_logos.find_one({"_id": _document_id(db)}) is not None


def seed_if_empty(db: Any, asset_ids: list[str]) -> bool:
    """Adopt style-seeded logos only when the merchant has no logo record yet."""
    if has_record(db) or not asset_ids:
        return False
    set_asset_ids(db, asset_ids)
    return True


def images(db: Any, merchant_id: str) -> list[dict[str, Any]]:
    """Logo entries for API responses; missing assets are skipped like references are."""
    entries: list[dict[str, Any]] = []
    for asset_id in get_asset_ids(db):
        asset = db.assets.find_one({"_id": asset_id})
        if asset is None:
            continue
        entries.append(
            {
                "asset_id": asset_id,
                "original_name": asset.get("original_name") or "",
                "url": asset.get("prepared_url") or asset.get("url") or asset_url(merchant_id, asset_id),
                "width": int(asset.get("width") or 0),
                "height": int(asset.get("height") or 0),
            }
        )
    return entries
