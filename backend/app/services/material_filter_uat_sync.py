"""Upload picked local material-filter copies to the deployed UAT asset library.

The browser cannot call the deployed API directly (different origin, CSRF and
httpOnly session), so the local backend forwards one photo at a time with the
AM Console session the operator pastes in. The cookie is only kept in the sync
document for the lifetime of the job and never logged.
"""

from __future__ import annotations

import io
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
from PIL import Image, ImageOps

from app.config import Settings
from app.services import material_filters as store
from app.services.material_filters import MaterialFilterError

SYNC_COLLECTION = "material_filter_syncs"
MAX_ASSET_TAGS = 5
CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".gif": "image/gif",
}
ACTIVE_STATUSES = ("queued", "running")


def now() -> datetime:
    return datetime.now(timezone.utc)


class SyncError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def base_url(settings: Settings, override: str = "") -> str:
    value = (override or settings.material_filter_uat_base_url or "").strip().rstrip("/")
    if not value:
        raise SyncError("uat_url_required", "请配置 UAT 地址（MATERIAL_FILTER_UAT_BASE_URL）")
    return value


def start_sync(
    db,
    settings: Settings,
    scheme: dict[str, Any],
    *,
    cookie: str,
    force: bool = False,
    limit: int = 0,
    dry_run: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    session = str(cookie or "").strip()
    if not session:
        raise SyncError("uat_cookie_required", "请先粘贴 AM 会话 Cookie（AMSessionId=…）", 422)
    active = db[SYNC_COLLECTION].find_one(
        {"scheme_id": scheme["_id"], "status": {"$in": list(ACTIVE_STATUSES)}}
    )
    if active:
        return active
    document = {
        "_id": store.new_id("msync"),
        "scheme_id": scheme["_id"],
        "folder_id": scheme.get("folder_id"),
        "status": "queued",
        "dry_run": bool(dry_run),
        "force": bool(force),
        "limit": int(limit) if limit else 0,
        "counters": {"total": 0, "done": 0, "uploaded": 0, "compressed": 0, "linked": 0, "failed": 0},
        "current": "",
        "error": None,
        "items": [],
        "created_at": now(),
        "started_at": None,
        "finished_at": None,
    }
    db[SYNC_COLLECTION].insert_one(document)
    if dry_run:
        _dry_plan(
            db,
            settings,
            scheme,
            str(document["_id"]),
            cookie=session,
            force=force,
            limit=limit,
            transport=transport,
        )
        return get_sync(db, str(document["_id"]))
    thread = threading.Thread(
        target=_guarded,
        args=(db, settings, scheme, str(document["_id"]), session, force, limit, transport),
        name=f"material-filter-sync-{document['_id']}",
        daemon=True,
    )
    thread.start()
    return get_sync(db, str(document["_id"]))


def _dry_plan(db, settings, scheme, sync_id, *, cookie, force, limit, transport) -> None:
    """Preview the target file names without uploading anything."""
    merchant_id = str(scheme.get("merchant_id") or db.merchant_id)
    root = base_url(settings)
    headers = {"Cookie": cookie, "Origin": root, "X-Media-Request": "1", "Accept": "application/json"}
    query: dict[str, Any] = {
        "scheme_id": scheme["_id"],
        "manual_pick": True,
        "copy_status": "copied",
    }
    if not force:
        query["uat_asset_id"] = None
    rows = list(db[store.EVALUATION_COLLECTION].find(query).sort("picked_at", 1))
    if limit:
        rows = rows[:limit]
    try:
        with httpx.Client(
            base_url=root, headers=headers, timeout=120, follow_redirects=True, transport=transport
        ) as client:
            used = _collect_names(client, merchant_id, "/assets") | _collect_names(
                client, merchant_id, "/am-assets"
            )
    except Exception as exc:  # noqa: BLE001
        raise SyncError("uat_unavailable", f"无法连接 UAT：{exc}", 502) from exc
    items = []
    for row in rows:
        suffix = Path(str(row.get("output_name") or "x.jpg")).suffix.lower() or ".jpg"
        target = _planned_name(row, suffix, used)
        base_name = target
        items.append(
            {
                "name": row["original_name"],
                "status": "planned",
                "asset_name": target,
                "renamed": target != f"{target.split('-')[0]}{suffix}",
            }
        )
    db[SYNC_COLLECTION].update_one(
        {"_id": sync_id},
        {
            "$set": {
                "status": "succeeded",
                "started_at": now(),
                "finished_at": now(),
                "counters": {"total": len(rows), "done": 0, "uploaded": 0, "compressed": 0, "linked": 0, "failed": 0},
                "items": items[-30:],
            }
        },
    )


def _guarded(db, settings, scheme, sync_id, cookie, force, limit, transport) -> None:
    try:
        run_sync(
            db,
            settings,
            scheme,
            sync_id,
            cookie=cookie,
            force=force,
            limit=limit,
            transport=transport,
        )
    except Exception as exc:  # noqa: BLE001 - poll must show the failure
        db[SYNC_COLLECTION].update_one(
            {"_id": sync_id},
            {"$set": {"status": "failed", "error": str(exc)[:500], "finished_at": now()}},
        )


def get_sync(db, sync_id: str) -> dict[str, Any]:
    document = db[SYNC_COLLECTION].find_one({"_id": sync_id})
    if not document:
        raise SyncError("sync_not_found", "同步任务不存在", 404)
    return document


def _planned_name(row: dict[str, Any], suffix: str, used: set[str]) -> str:
    from app.services.material_filter_files import sanitize_output_stem

    stem = sanitize_output_stem(
        row.get("display_name") or row.get("original_name") or "图片", max_length=60
    )
    candidate = stem
    index = 2
    while f"{candidate}{suffix}".casefold() in used:
        candidate = f"{stem}-{index}"
        index += 1
    used.add(f"{candidate}{suffix}".casefold())
    return f"{candidate}{suffix}"


def fit_upload_bytes(data: bytes, max_bytes: int) -> tuple[bytes, str, bool]:
    if max_bytes <= 0 or len(data) <= max_bytes:
        return data, "", False
    with Image.open(io.BytesIO(data)) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    current = image
    encoded = b""
    for _ in range(6):
        for quality in (92, 88, 84, 80, 76, 72, 68, 64):
            buffer = io.BytesIO()
            current.save(buffer, format="JPEG", quality=quality, optimize=True)
            encoded = buffer.getvalue()
            if len(encoded) <= max_bytes:
                return encoded, ".jpg", True
        current = current.resize(
            (max(256, int(current.width * 0.85)), max(256, int(current.height * 0.85))),
            Image.Resampling.LANCZOS,
        )
    return encoded, ".jpg", True


def desired_tags(row: dict[str, Any]) -> list[str]:
    tags = [str(tag).strip() for tag in row.get("tags") or [] if str(tag).strip()]
    return list(dict.fromkeys(tags))[: MAX_ASSET_TAGS - 1] + ["素材筛选"]


def _collect_names(client: httpx.Client, merchant_id: str, path: str) -> set[str]:
    names: set[str] = set()
    skip = 0
    while True:
        try:
            response = client.get(f"/api/merchants/{merchant_id}{path}", params={"skip": skip, "limit": 100})
        except Exception:  # noqa: BLE001
            break
        if response.status_code != 200:
            break
        payload = response.json()
        items = payload if isinstance(payload, list) else payload.get("items") or []
        for item in items:
            name = str(item.get("original_name") or "").strip()
            if name:
                names.add(name.casefold())
        total = payload.get("total") if isinstance(payload, dict) else None
        skip += len(items)
        if not items or len(items) < 100 or (total is not None and skip >= int(total)):
            break
    return names


def _find_am_media(client: httpx.Client, merchant_id: str, original_name: str) -> dict[str, Any] | None:
    stem = Path(original_name).stem.casefold()
    if not stem:
        return None
    try:
        response = client.get(
            f"/api/merchants/{merchant_id}/am-assets", params={"keyword": stem, "limit": 50}
        )
        if response.status_code != 200:
            return None
        items = (response.json() or {}).get("items") or []
    except Exception:  # noqa: BLE001
        return None
    for item in items:
        if stem in str(item.get("original_name") or "").casefold():
            return item
    return None


def run_sync(
    db,
    settings: Settings,
    scheme: dict[str, Any],
    sync_id: str,
    *,
    cookie: str,
    force: bool = False,
    limit: int = 0,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    merchant_id = str(scheme.get("merchant_id") or db.merchant_id)
    root = base_url(settings)
    headers = {
        "Cookie": cookie,
        "Origin": root,
        "X-Media-Request": "1",
        "Accept": "application/json",
    }
    query: dict[str, Any] = {
        "scheme_id": scheme["_id"],
        "manual_pick": True,
        "copy_status": "copied",
    }
    if not force:
        query["uat_asset_id"] = None
    rows = list(db[store.EVALUATION_COLLECTION].find(query).sort("picked_at", 1))
    if limit:
        rows = rows[:limit]

    db[SYNC_COLLECTION].update_one(
        {"_id": sync_id},
        {"$set": {"status": "running", "started_at": now(), "counters.total": len(rows)}},
    )
    if not rows:
        db[SYNC_COLLECTION].update_one(
            {"_id": sync_id},
            {"$set": {"status": "succeeded", "finished_at": now(), "current": ""}},
        )
        return get_sync(db, sync_id)

    counters = {"total": len(rows), "done": 0, "uploaded": 0, "compressed": 0, "linked": 0, "failed": 0}
    items: list[dict[str, Any]] = []
    max_file_bytes = int(settings.material_filter_uat_max_body_mb * 1024 * 1024 * 3 / 4)
    try:
        client = httpx.Client(
            base_url=root, headers=headers, timeout=300, follow_redirects=True, transport=transport
        )
    except Exception as exc:  # noqa: BLE001
        raise SyncError("uat_unavailable", f"无法连接 UAT：{exc}", 502) from exc

    with client:
        used = _collect_names(client, merchant_id, "/assets") | _collect_names(
            client, merchant_id, "/am-assets"
        )
        for row in rows:
            label = row["original_name"]
            db[SYNC_COLLECTION].update_one({"_id": sync_id}, {"$set": {"current": label}})
            try:
                path = store.scheme_directory(settings, scheme) / str(row["output_path"])
                if not path.is_file():
                    raise SyncError("output_missing", f"本机副本不存在：{path.name}", 404)
                data, new_suffix, recompressed = fit_upload_bytes(path.read_bytes(), max_file_bytes)
                suffix = new_suffix or path.suffix.lower() or ".jpg"
                file_name = _planned_name(row, suffix, used)
                response = client.post(
                    f"/api/merchants/{merchant_id}/assets",
                    files={"file": (file_name, data, CONTENT_TYPES.get(suffix, "application/octet-stream"))},
                )
                if response.status_code not in {200, 201}:
                    detail = response.text[:200]
                    existing = None
                    if response.status_code >= 500:
                        existing = _find_am_media(client, merchant_id, row["original_name"])
                    if existing:
                        _record_linked(db, row, existing)
                        counters["linked"] += 1
                        items.append({"name": label, "status": "linked", "message": existing.get("original_name")})
                        continue
                    raise SyncError("uat_upload_failed", f"HTTP {response.status_code}: {detail}", 502)
                asset = response.json()
                asset_id = str(asset.get("id") or "")
                if asset_id.startswith("asset_"):
                    tag_response = client.put(
                        f"/api/merchants/{merchant_id}/assets/{asset_id}/tags",
                        json={"tags": desired_tags(row)},
                    )
                    tag_error = None if tag_response.status_code < 300 else tag_response.text[:200]
                else:
                    tag_error = None
                db[store.EVALUATION_COLLECTION].update_one(
                    {"_id": row["_id"]},
                    {
                        "$set": {
                            "uat_asset_id": asset_id,
                            "uat_asset_name": asset.get("original_name") or file_name,
                            "uat_asset_url": asset.get("url"),
                            "uat_synced_at": now(),
                            "uat_sync_error": None,
                            "uat_sync_note": None,
                            "uat_tag_error": tag_error,
                            "uat_recompressed": recompressed,
                        }
                    },
                )
                counters["done"] += 1
                counters["uploaded"] += 1
                if recompressed:
                    counters["compressed"] += 1
                items.append(
                    {
                        "name": label,
                        "status": "uploaded",
                        "asset_name": asset.get("original_name") or file_name,
                        "asset_id": asset_id,
                        "compressed": recompressed,
                        "tag_error": tag_error,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - keep going, report per item
                counters["failed"] += 1
                db[store.EVALUATION_COLLECTION].update_one(
                    {"_id": row["_id"]}, {"$set": {"uat_sync_error": str(exc)[:500]}}
                )
                items.append({"name": label, "status": "failed", "message": str(exc)[:300]})
            finally:
                counters["done"] = counters["uploaded"] + counters["linked"] + counters["failed"]
                db[SYNC_COLLECTION].update_one(
                    {"_id": sync_id},
                    {"$set": {"counters": dict(counters), "items": items[-30:]}},
                )
        status = "succeeded" if counters["failed"] == 0 else (
            "partial" if counters["uploaded"] or counters["linked"] else "failed"
        )
        db[SYNC_COLLECTION].update_one(
            {"_id": sync_id},
            {
                "$set": {
                    "status": status,
                    "current": "",
                    "finished_at": now(),
                    "counters": counters,
                    "items": items[-30:],
                }
            },
        )
    return get_sync(db, sync_id)


def _record_linked(db, row: dict[str, Any], existing: dict[str, Any]) -> None:
    db[store.EVALUATION_COLLECTION].update_one(
        {"_id": row["_id"]},
        {
            "$set": {
                "uat_asset_id": str(existing.get("id") or ""),
                "uat_asset_name": existing.get("original_name"),
                "uat_asset_url": existing.get("url"),
                "uat_synced_at": now(),
                "uat_sync_error": None,
                "uat_sync_note": "AM 资源库已存在同内容素材，已关联",
            }
        },
    )


def sync_out(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": document["_id"],
        "scheme_id": document.get("scheme_id"),
        "status": document.get("status"),
        "dry_run": bool(document.get("dry_run")),
        "counters": document.get("counters") or {},
        "current": document.get("current") or "",
        "error": document.get("error"),
        "items": document.get("items") or [],
        "created_at": document.get("created_at"),
        "started_at": document.get("started_at"),
        "finished_at": document.get("finished_at"),
    }
