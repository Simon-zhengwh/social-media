from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from app.api_paths import asset_url
from app.services import asset_records, storage
from app.services.am_client import AMClient, AMClientError
from app.services.object_store import ObjectStore

AM_ASSET_PREFIX = "am:"


def is_am_asset_id(asset_id: str) -> bool:
    return str(asset_id).startswith(AM_ASSET_PREFIX)


def list_media(
    am: AMClient,
    cookie: str,
    merchant_id: str,
    *,
    keyword: str,
    skip: int,
    limit: int,
) -> dict[str, Any]:
    response = am.search_media(cookie, merchant_id, keyword=keyword, skip=skip, limit=limit)
    items = []
    for media in response.get("media_libraries") or []:
        if not isinstance(media, dict) or not media.get("id"):
            continue
        items.append(
            {
                "id": f"{AM_ASSET_PREFIX}{media['id']}",
                "am_media_library_id": str(media["id"]),
                "original_name": str(media.get("name") or media["id"]),
                "width": 0,
                "height": 0,
                "mime_type": _mime_type(media),
                "url": str(media.get("thumbnail_image_url") or media.get("url") or ""),
                "created_at": media.get("updated_time"),
                "source": "am",
                "read_only": True,
            }
        )
    return {
        "items": items,
        "total": int(response.get("total") or 0),
        "skip": skip,
        "limit": limit,
    }


def list_local(
    db: Any,
    merchant_id: str,
    *,
    keyword: str,
    skip: int,
    limit: int,
) -> dict[str, Any]:
    """Uploaded assets only, for runs that have no AM Console session."""
    local_query = _local_query(keyword)
    total = db.assets.count_documents(local_query)
    docs = list(db.assets.find(local_query).sort("created_at", -1).skip(skip).limit(limit))
    return {
        "items": [_local_item(merchant_id, document) for document in docs],
        "total": total,
        "skip": skip,
        "limit": limit,
    }


def _local_query(keyword: str) -> dict[str, Any]:
    query: dict[str, Any] = {"source": "upload"}
    normalized = keyword.strip()
    if normalized:
        query["original_name"] = {"$regex": re.escape(normalized), "$options": "i"}
    return query


def _local_item(merchant_id: str, document: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(document["_id"]),
        "original_name": str(document.get("original_name") or ""),
        "width": int(document.get("width") or 0),
        "height": int(document.get("height") or 0),
        "mime_type": str(document.get("mime_type") or "image/png"),
        "url": document.get("prepared_url")
        or document.get("url")
        or asset_url(merchant_id, str(document["_id"])),
        "created_at": document.get("created_at"),
        "source": "upload",
        "read_only": False,
    }


def list_for_merchant(
    db: Any,
    am: AMClient,
    cookie: str,
    merchant_id: str,
    *,
    keyword: str,
    skip: int,
    limit: int,
) -> dict[str, Any]:
    local_query = _local_query(keyword)

    local_total = db.assets.count_documents(local_query)
    local_docs = []
    if skip < local_total:
        local_docs = list(
            db.assets.find(local_query)
            .sort("created_at", -1)
            .skip(skip)
            .limit(limit)
        )
    local_items = [_local_item(merchant_id, document) for document in local_docs]

    remaining = limit - len(local_items)
    am_page = list_media(
        am,
        cookie,
        merchant_id,
        keyword=keyword,
        skip=max(0, skip - local_total),
        limit=remaining if remaining > 0 else 1,
    )
    am_items = am_page["items"] if remaining > 0 else []
    return {
        "items": [*local_items, *am_items],
        "total": local_total + int(am_page["total"]),
        "skip": skip,
        "limit": limit,
    }


def import_if_needed(
    db: Any,
    store: ObjectStore,
    settings: Any,
    am: AMClient,
    cookie: str,
    merchant_id: str,
    asset_id: str,
) -> str:
    if not is_am_asset_id(asset_id):
        return asset_id
    media_id = asset_id[len(AM_ASSET_PREFIX):]
    existing = db.assets.find_one({"source": "am", "am_media_library_id": media_id})
    if existing is not None:
        return str(existing["_id"])

    media = am.get_media(cookie, merchant_id, media_id)
    url = str(media.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise AMClientError(502, "am_asset_url_invalid", "AM 素材没有可下载地址")
    try:
        response = am.http.get(url, follow_redirects=True)
    except httpx.RequestError as exc:
        raise AMClientError(502, "am_asset_download_failed", "下载 AM 素材失败") from exc
    if response.status_code != 200:
        raise AMClientError(502, "am_asset_download_failed", f"下载 AM 素材失败（HTTP {response.status_code}）")
    data = response.content
    if len(data) > settings.max_upload_bytes:
        raise AMClientError(413, "am_asset_too_large", "AM 素材超过允许导入的大小")
    prepared = storage.prepare_upload(settings, store, data, str(media.get("name") or f"{media_id}.png"))
    document = asset_records.asset_document(prepared)
    document.update(
        {
            "source": "am",
            "am_media_library_id": media_id,
            "am_file_key": media.get("key"),
            "am_original_url": url,
            "imported_at": datetime.now(timezone.utc),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    )
    db.assets.insert_one(document)
    return str(document["_id"])


def _mime_type(media: dict[str, Any]) -> str:
    media_format = str(media.get("media_library_format") or "").upper()
    return {
        "JPEG": "image/jpeg",
        "JPG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
        "GIF": "image/gif",
        "HEIC": "image/heic",
    }.get(media_format, "image/jpeg")
