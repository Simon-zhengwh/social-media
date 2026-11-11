from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

from app.api_paths import asset_url
from app.services import asset_records, storage
from app.services.am_client import AMClient, AMClientError
from app.services.object_store import ObjectStore

AM_ASSET_PREFIX = "am:"

# AM rejects more than five tags per record and rewrites the whole array on update.
MAX_ASSET_TAGS = 5
MAX_TAG_LENGTH = 40


def is_am_asset_id(asset_id: str) -> bool:
    return str(asset_id).startswith(AM_ASSET_PREFIX)


def normalize_tags(tags: Iterable[Any]) -> list[str]:
    """Trim, drop empties and de-duplicate case-insensitively, like AM does."""
    result: list[str] = []
    for raw in tags or []:
        tag = str(raw).strip()[:MAX_TAG_LENGTH]
        if not tag:
            continue
        if any(item.lower() == tag.lower() for item in result):
            continue
        result.append(tag)
    if len(result) > MAX_ASSET_TAGS:
        raise ValueError(f"每张素材最多 {MAX_ASSET_TAGS} 个 Tag")
    return result


def media_tags(media: dict[str, Any]) -> list[str]:
    raw = media.get("tags")
    if not isinstance(raw, list):
        return []
    return [str(tag).strip() for tag in raw if str(tag).strip()]


def list_media(
    am: AMClient,
    cookie: str,
    merchant_id: str,
    *,
    keyword: str,
    tag: str = "",
    skip: int,
    limit: int,
) -> dict[str, Any]:
    response = am.search_media(
        cookie, merchant_id, keyword=keyword, tag=tag, skip=skip, limit=limit
    )
    items = []
    for media in response.get("media_libraries") or []:
        if not isinstance(media, dict) or not media.get("id"):
            continue
        items.append(_am_item(media))
    return {
        "items": items,
        "total": int(response.get("total") or 0),
        "skip": skip,
        "limit": limit,
    }


def _am_item(media: dict[str, Any], local: dict[str, Any] | None = None) -> dict[str, Any]:
    """One row for an AM record, resolved to our asset id when an upload mapping exists."""
    item = {
        "id": f"{AM_ASSET_PREFIX}{media['id']}",
        "am_media_library_id": str(media["id"]),
        "original_name": str(media.get("name") or media["id"]),
        "width": 0,
        "height": 0,
        "mime_type": _mime_type(media),
        "url": str(media.get("thumbnail_image_url") or media.get("url") or ""),
        "created_at": media.get("updated_time"),
        "source": "am",
        # Deleting an AM media-library record is not an operation this app was granted.
        "read_only": True,
        "tags": media_tags(media),
    }
    if local is not None:
        # Our own upload: keep the internal id so tasks referencing it still resolve, and prefer
        # the local preview that the poster renderer already knows how to read.
        item.update(
            {
                "id": str(local["_id"]),
                "original_name": str(local.get("original_name") or item["original_name"]),
                "width": int(local.get("width") or 0),
                "height": int(local.get("height") or 0),
                "mime_type": str(local.get("mime_type") or item["mime_type"]),
                "source": "upload",
            }
        )
    return item


def list_local(
    db: Any,
    merchant_id: str,
    *,
    keyword: str,
    tag: str = "",
    skip: int,
    limit: int,
) -> dict[str, Any]:
    """Uploaded assets only, for runs that have no AM Console session."""
    local_query = _local_query(keyword, tag, registered_in_am=False)
    total = db.assets.count_documents(local_query)
    docs = list(db.assets.find(local_query).sort("created_at", -1).skip(skip).limit(limit))
    return {
        "items": [_local_item(merchant_id, document) for document in docs],
        "total": total,
        "skip": skip,
        "limit": limit,
    }


def _local_query(keyword: str, tag: str, *, registered_in_am: bool) -> dict[str, Any]:
    query: dict[str, Any] = asset_records.uploaded_asset_query()
    if not registered_in_am:
        # Rows that already exist as AM media come back from AM search; listing them here too
        # would show and count the same image twice.
        query["am_media_library_id"] = {"$exists": False}
    normalized = keyword.strip()
    if normalized:
        query["original_name"] = {"$regex": re.escape(normalized), "$options": "i"}
    # Same semantics as AM: case-insensitive substring match against the tag array.
    tag_query = tag.strip()
    if tag_query:
        query["tags"] = {"$regex": re.escape(tag_query), "$options": "i"}
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
        "tags": [str(tag) for tag in document.get("tags") or [] if str(tag).strip()],
    }


def list_for_merchant(
    db: Any,
    am: AMClient,
    cookie: str,
    merchant_id: str,
    *,
    keyword: str,
    tag: str = "",
    skip: int,
    limit: int,
) -> dict[str, Any]:
    local_query = _local_query(keyword, tag, registered_in_am=False)

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
        tag=tag,
        skip=max(0, skip - local_total),
        limit=remaining if remaining > 0 else 1,
    )
    am_items = am_page["items"] if remaining > 0 else []
    if am_items:
        am_items = _attach_local_ids(db, am_items)
    return {
        "items": [*local_items, *am_items],
        "total": local_total + int(am_page["total"]),
        "skip": skip,
        "limit": limit,
    }


def _attach_local_ids(db: Any, am_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Swap AM ids for our ids on records this project uploaded itself."""
    media_ids = [item["am_media_library_id"] for item in am_items]
    mappings = {
        str(document.get("am_media_library_id")): document
        for document in db.assets.find({"am_media_library_id": {"$in": media_ids}})
    }
    return [
        _resolve_item(item, mappings.get(item["am_media_library_id"])) for item in am_items
    ]


def _resolve_item(item: dict[str, Any], local: dict[str, Any] | None) -> dict[str, Any]:
    if local is None:
        return item
    return {
        **item,
        "id": str(local["_id"]),
        "original_name": str(local.get("original_name") or item["original_name"]),
        "width": int(local.get("width") or 0),
        "height": int(local.get("height") or 0),
        "mime_type": str(local.get("mime_type") or item["mime_type"]),
        "source": "upload",
    }


def update_tags(
    db: Any,
    am: AMClient | None,
    cookie: str,
    merchant_id: str,
    asset_id: str,
    tags: Iterable[Any],
) -> dict[str, Any]:
    """Replace one asset's tags in AM (deployed) or in the local record (bypass)."""
    normalized = normalize_tags(tags)
    now = datetime.now(timezone.utc)

    if is_am_asset_id(asset_id):
        media_id = asset_id[len(AM_ASSET_PREFIX):]
        if am is None:
            raise AMClientError(503, "am_unavailable", "本地模式不能修改 AM 素材的 Tag")
        media = am.update_media_tags(cookie, merchant_id, media_id, normalized)
        return _am_item(media, db.assets.find_one({"am_media_library_id": media_id}))

    document = db.assets.find_one({"_id": asset_id})
    if document is None:
        raise LookupError(asset_id)
    media_id = str(document.get("am_media_library_id") or "")
    if media_id and am is not None:
        # AM is the source of truth for a registered upload; a local-only write would fork it.
        media = am.update_media_tags(cookie, merchant_id, media_id, normalized)
        db.assets.update_one(
            {"_id": asset_id},
            {"$set": {"tags": normalized, "tags_updated_at": now}},
        )
        return _am_item(media, {**document, "tags": normalized})
    db.assets.update_one(
        {"_id": asset_id},
        {"$set": {"tags": normalized, "tags_updated_at": now}},
    )
    updated = db.assets.find_one({"_id": asset_id}) or document
    return _local_item(merchant_id, updated)


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


def register_in_library(
    db: Any,
    am: AMClient,
    cookie: str,
    merchant_id: str,
    document: dict[str, Any],
    data: bytes,
    original_name: str,
) -> dict[str, Any]:
    """Create the AM media-library record for an upload and remember the mapping."""
    media = am.upload_media(
        cookie,
        merchant_id,
        filename=original_name,
        data=data,
        mime_type=str(document.get("mime_type") or "image/png"),
    )
    db.assets.update_one(
        {"_id": document["_id"]},
        {
            "$set": {
                "am_media_library_id": str(media["id"]),
                "am_file_key": media.get("key"),
                "am_uploaded_at": datetime.now(timezone.utc),
            }
        },
    )
    return {**document, "am_media_library_id": str(media["id"])}


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
