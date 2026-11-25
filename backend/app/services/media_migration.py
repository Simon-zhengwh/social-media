"""One-way AM Console media-library migration from UAT to Prod.

This is a local operator tool, not a product feature: the browser pastes both AM
Console sessions, the local backend reads the UAT library, copies the missing
images into the Prod library and checkpoints every upload in the local MongoDB
so a restart resumes where it stopped and past runs stay inspectable.

Two rules make the tool safe to run against production:

* the sessions live in memory for the lifetime of one request or one background
  job; they are never written to MongoDB, a checkpoint or a log line;
* image bytes are downloaded from the signed CDN URL with a clean client, so the
  AM session is never sent to object storage and a signature is never broken.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pymongo.errors import PyMongoError

from app.config import Settings
# Reuse the tested byte-fitting and cookie handling of the material-filter sync:
# both tools talk to the same AM Console endpoints and must not drift apart.
from app.services.material_filter_uat_sync import fit_upload_bytes, session_cookie
from app.time_utils import utc_iso

logger = logging.getLogger(__name__)

PAGE_LIMIT = 100
MAX_TAGS = 5
ITEM_COLLECTION = "media_migration_items"
JOB_COLLECTION = "media_migration_jobs"
# AM's media-library validation only accepts these containers.
SUPPORTED_FORMATS = frozenset({"JPEG", "PNG", "SVG", "GIF"})
FORMAT_SUFFIX = {"JPEG": ".jpg", "PNG": ".png", "SVG": ".svg", "GIF": ".gif"}
SUFFIX_FORMAT = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".jfif": "JPEG",
    ".png": "PNG",
    ".svg": "SVG",
    ".gif": "GIF",
}
ACTIVE_STATUSES = ("queued", "running")
# A rejected session fails every remaining image the same way; stop instead of hammering Prod.
AUTH_CODES = frozenset(
    {"uat_cookie_invalid", "uat_access_rejected", "prod_cookie_invalid", "prod_access_rejected"}
)
CONNECTION_ERRORS = (
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.RemoteProtocolError,
)

DEFAULT_UAT_BASE_URL = "https://am.connexup-uat.net"
DEFAULT_PROD_BASE_URL = "https://am.connexup.com"
INTERRUPTED_MESSAGE = "后端服务重启，迁移已中断；再次点击「开始迁移」只上传尚未迁移的素材。"

# Live jobs: only the sessions, the cancel flag and the working state live here.
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def now() -> datetime:
    return datetime.now(timezone.utc)


class MediaMigrationError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


# --- request spec ---------------------------------------------------------


class MigrationSpec:
    """Validated request inputs. ``uat_cookie``/``prod_cookie`` never leave memory."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.uat_base_url = _base_url(payload.get("uat_base_url"), "UAT 地址")
        self.prod_base_url = _base_url(payload.get("prod_base_url"), "Prod 地址")
        self.uat_merchant_id = _merchant_id(payload.get("uat_merchant_id"), "UAT Merchant ID")
        self.prod_merchant_id = _merchant_id(payload.get("prod_merchant_id"), "Prod Merchant ID")
        self.uat_cookie = _cookie(payload.get("uat_cookie"), "UAT")
        self.prod_cookie = _cookie(payload.get("prod_cookie"), "Prod")
        self.skip_same_name = bool(payload.get("skip_same_name", True))
        self.force = bool(payload.get("force", False))
        limit = payload.get("limit", 0) or 0
        try:
            self.limit = max(0, int(limit))
        except (TypeError, ValueError):
            raise MediaMigrationError("invalid_limit", "limit 必须是整数") from None

    @property
    def scope(self) -> dict[str, str]:
        """The pair that owns every checkpoint row; changing either side starts fresh."""
        return {
            "uat_base_url": self.uat_base_url,
            "uat_merchant_id": self.uat_merchant_id,
            "prod_base_url": self.prod_base_url,
            "prod_merchant_id": self.prod_merchant_id,
        }


def _base_url(value: Any, label: str) -> str:
    url = str(value or "").strip().rstrip("/")
    if not url:
        raise MediaMigrationError("base_url_required", f"请填写 {label}")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise MediaMigrationError("invalid_base_url", f"{label} 必须是以 http(s):// 开头的完整地址")
    return url


def _merchant_id(value: Any, label: str) -> str:
    merchant_id = str(value or "").strip()
    if not merchant_id:
        raise MediaMigrationError("merchant_id_required", f"请填写 {label}")
    return merchant_id


def _cookie(value: Any, label: str) -> str:
    cookie = session_cookie(str(value or ""))
    if not cookie:
        raise MediaMigrationError("cookie_required", f"请粘贴 {label} AM Console Cookie")
    return cookie


def defaults(settings: Settings) -> dict[str, str]:
    return {
        "uat_base_url": (settings.am_base_url or "").strip().rstrip("/") or DEFAULT_UAT_BASE_URL,
        "prod_base_url": DEFAULT_PROD_BASE_URL,
    }


# --- checkpoint ledger (local MongoDB) ------------------------------------


MONGO_UNAVAILABLE_MESSAGE = (
    "本机 MongoDB 不可用（127.0.0.1:27018），请先启动 Docker Desktop 并执行 docker compose up -d"
)


def _mongo_error(exc: PyMongoError) -> MediaMigrationError:
    return MediaMigrationError("mongo_unavailable", MONGO_UNAVAILABLE_MESSAGE, 503)


def load_ledger(db, spec: MigrationSpec) -> dict[str, dict[str, Any]]:
    """Uploaded checkpoints for one UAT→Prod pair, keyed by the UAT media id."""
    try:
        rows = list(db[ITEM_COLLECTION].find(spec.scope))
    except PyMongoError as exc:
        raise _mongo_error(exc) from exc
    return {str(row.get("source_media_id") or ""): row for row in rows}


def record_item(
    db,
    spec: MigrationSpec,
    item: dict[str, Any],
    *,
    status: str,
    job_id: str,
    prod_media_id: str = "",
    uploaded_filename: str = "",
    sha256: str = "",
    size: int = 0,
    compressed: bool = False,
    error: str | None = None,
) -> None:
    """Checkpoint one image the moment it settles, so a restart never re-uploads it."""
    stamp = now()
    fields: dict[str, Any] = {
        "source_media_id": item["source_id"],
        "status": status,
        "name": item.get("name") or "",
        "format": item.get("format") or "",
        "tags": list(item.get("tags") or []),
        "prod_media_id": prod_media_id,
        "uploaded_filename": uploaded_filename,
        "sha256": sha256,
        "bytes": int(size),
        "compressed": bool(compressed),
        "error": error,
        "last_job_id": job_id,
        "updated_at": stamp,
    }
    if status == "uploaded":
        fields["uploaded_at"] = stamp
    try:
        db[ITEM_COLLECTION].update_one(
            {**spec.scope, "source_media_id": item["source_id"]}, {"$set": fields}, upsert=True
        )
    except PyMongoError as exc:
        raise _mongo_error(exc) from exc


# --- AM Console calls -----------------------------------------------------


def am_client(base_url: str, cookie: str, transport: httpx.BaseTransport | None) -> httpx.Client:
    """A client that carries the AM session, exactly like the console's own AJAX calls."""
    return httpx.Client(
        base_url=base_url,
        headers={
            "Cookie": cookie,
            "Accept": "application/json",
            "Origin": base_url,
        },
        timeout=120,
        follow_redirects=True,
        transport=transport,
    )


def download_client(transport: httpx.BaseTransport | None) -> httpx.Client:
    """A client without any AM session: image URLs are signed CDN links, not AM endpoints."""
    return httpx.Client(timeout=180, follow_redirects=True, transport=transport)


def _reject_auth(response: httpx.Response, label: str, prefix: str) -> None:
    if response.status_code not in {401, 403}:
        return
    detail = ""
    try:
        error = response.json().get("error") or {}
        detail = str(error.get("message") or error.get("code") or "")[:120]
    except (ValueError, AttributeError):
        detail = ""
    suffix = f"：{detail}" if detail else ""
    if response.status_code == 401:
        raise MediaMigrationError(
            f"{prefix}_cookie_invalid",
            f"{label} Cookie 无效或已过期（HTTP 401{suffix}）",
            401,
        )
    raise MediaMigrationError(
        f"{prefix}_access_rejected",
        f"{label} 拒绝访问该商户（HTTP 403{suffix}）",
        403,
    )


def _media_items(payload: Any) -> list[dict[str, Any]]:
    raw = payload.get("media_libraries") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    items: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("id") or "").strip()
        url = str(item.get("url") or "").strip()
        kind = str(item.get("media_library_type") or "IMAGE").strip().upper()
        if kind != "IMAGE" or not source_id or not url:
            continue
        items.append(item)
    return items


def search_images(
    client: httpx.Client,
    merchant_id: str,
    *,
    label: str,
    prefix: str,
) -> list[dict[str, Any]]:
    """Every IMAGE record of one merchant, paged until the list stops moving."""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    skip = 0
    total = 0
    while True:
        try:
            response = client.put(
                "/ajax/media-library/search",
                json={
                    "merchant_id": merchant_id,
                    "media_library_type": "IMAGE",
                    "skip": skip,
                    # AM Console validation caps the page size at 100.
                    "limit": PAGE_LIMIT,
                },
            )
        except CONNECTION_ERRORS as exc:
            raise MediaMigrationError(
                f"{prefix}_unavailable", f"无法连接 {label}（{exc.__class__.__name__}）", 502
            ) from exc
        _reject_auth(response, label, prefix)
        if response.status_code >= 400:
            raise MediaMigrationError(
                f"{prefix}_search_failed",
                f"{label} 素材库读取失败（HTTP {response.status_code}）",
                502,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MediaMigrationError(
                f"{prefix}_invalid_response", f"{label} 素材库响应不是有效 JSON", 502
            ) from exc
        page_items = _media_items(payload)
        try:
            total = int(payload.get("total") or 0) if isinstance(payload, dict) else 0
        except (TypeError, ValueError):
            total = 0
        fresh = [item for item in page_items if str(item["id"]).strip() not in seen]
        if not fresh:
            # An empty page, or one that only repeats ids already collected, ends the paging.
            break
        for item in fresh:
            seen.add(str(item["id"]).strip())
        items.extend(fresh)
        skip += max(len(page_items), len(fresh))
        if total and len(seen) >= total:
            break
    return items


def _media_format(item: dict[str, Any]) -> str:
    declared = str(item.get("media_library_format") or "").strip().upper()
    if declared:
        return declared
    suffix = Path(str(item.get("name") or "")).suffix.lower()
    return SUFFIX_FORMAT.get(suffix, "")


def _name_key(value: str) -> tuple[str, str]:
    name = str(value or "").strip()
    return name.casefold(), Path(name).stem.strip().casefold()


def _migration_order(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Oldest first, so the Prod library ends up in the same order as UAT."""
    stamped = all(str(item.get("updated_time") or "").strip() for item in items)
    if stamped:
        return sorted(items, key=lambda item: str(item.get("updated_time") or ""))
    # AM lists the newest record first, so the reversed list is roughly oldest-first.
    return list(reversed(items))


def _tags(item: dict[str, Any]) -> list[str]:
    values = [str(tag).strip() for tag in item.get("tags") or [] if str(tag).strip()]
    return list(dict.fromkeys(values))


def _public(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if not key.startswith("_")}


# --- diff / preview -------------------------------------------------------


def preview(
    settings: Settings,
    db,
    payload: dict[str, Any],
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    spec = MigrationSpec(payload)
    return public_preview(plan(settings, db, spec, transport=transport))


def public_preview(result: dict[str, Any]) -> dict[str, Any]:
    """Drop the internal download URLs from an API answer."""
    return {key: value for key, value in result.items() if not key.startswith("_")}


def plan(
    settings: Settings,
    db,
    spec: MigrationSpec,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    with am_client(spec.uat_base_url, spec.uat_cookie, transport) as uat:
        uat_items = search_images(uat, spec.uat_merchant_id, label="UAT", prefix="uat")
    with am_client(spec.prod_base_url, spec.prod_cookie, transport) as prod:
        prod_items = search_images(prod, spec.prod_merchant_id, label="Prod", prefix="prod")

    ledger = load_ledger(db, spec)
    prod_ids = {str(item.get("id") or "").strip() for item in prod_items}
    prod_names = {key for item in prod_items for key in _name_key(str(item.get("name") or "")) if key}

    items: list[dict[str, Any]] = []
    counters = {"planned": 0, "skipped_ledger": 0, "skipped_same_name": 0}
    for item in _migration_order(uat_items):
        source_id = str(item["id"]).strip()
        name = str(item.get("name") or "").strip()
        media_format = _media_format(item)
        tags = _tags(item)
        entry = ledger.get(source_id) or {}
        prod_media_id = str(entry.get("prod_media_id") or "").strip()
        recorded = str(entry.get("status") or "") == "uploaded"
        status = "planned"
        reason = ""
        if not spec.force and recorded and prod_media_id and prod_media_id in prod_ids:
            status = "skipped_ledger"
            reason = "已同步过，Prod 中仍存在"
        elif not spec.force and recorded and prod_media_id:
            # The operator deleted the record in Prod: upload it again.
            status = "planned"
            reason = "Prod 中已被删除，需要重新上传"
            prod_media_id = ""
        elif not spec.force and spec.skip_same_name:
            stem_key = _name_key(name)[1]
            if _name_key(name)[0] in prod_names or (stem_key and stem_key in prod_names):
                status = "skipped_same_name"
                reason = "Prod 已有同名素材"
                prod_media_id = ""
        counters[status] += 1
        items.append(
            {
                "source_id": source_id,
                "name": name,
                "format": media_format,
                "tags": tags,
                "thumbnail_url": str(item.get("thumbnail_image_url") or ""),
                "status": status,
                "prod_media_id": prod_media_id or None,
                "reason": reason,
                "_url": str(item.get("url") or "").strip(),
            }
        )
    return {
        "uat_total": len(uat_items),
        "prod_total": len(prod_items),
        **counters,
        "items": [_public(item) for item in items],
        "_items": items,
    }


# --- upload preparation ---------------------------------------------------


def upload_body_max_bytes(settings: Settings) -> int:
    """Raw bytes that still fit AM's JSON body budget once base64 inflates them."""
    budget = max(0, int(settings.am_upload_max_body_bytes) - 8192)
    return int(budget * 3 / 4)


def _filename(name: str, media_format: str) -> str:
    raw = str(name or "").replace("\\", "/").split("/")[-1].strip()
    stem = Path(raw).stem.strip() if raw else ""
    stem = "".join(char for char in stem if char.isprintable()).strip() or "image"
    return f"{stem[:120]}{FORMAT_SUFFIX[media_format]}"


def prepare_upload(data: bytes, item: dict[str, Any], settings: Settings) -> tuple[bytes, str, str]:
    """Downscale/re-encode when AM cannot take the bytes as they are.

    Returns ``(bytes, media_library_format, filename)``.
    """
    media_format = str(item.get("format") or "").upper()
    max_bytes = upload_body_max_bytes(settings)
    if media_format not in SUPPORTED_FORMATS:
        # WEBP/HEIC and friends are not an AM media format at all; JPEG always is.
        data, _, _ = fit_upload_bytes(data, max_bytes, force_jpeg=True)
        return data, "JPEG", _filename(str(item.get("name") or ""), "JPEG")
    if len(data) > max_bytes:
        if media_format == "SVG":
            raise MediaMigrationError(
                "image_too_large", f"SVG 超过 AM 上传体积上限（{len(data)} 字节），无法压缩"
            )
        data, _, reencoded = fit_upload_bytes(data, max_bytes)
        if reencoded:
            return data, "JPEG", _filename(str(item.get("name") or ""), "JPEG")
    return data, media_format, _filename(str(item.get("name") or ""), media_format)


def upload_image(
    client: httpx.Client,
    merchant_id: str,
    *,
    filename: str,
    data: bytes,
    media_format: str,
    tags: list[str],
    max_body_bytes: int,
) -> dict[str, Any]:
    body = {
        "category": "MEDIA_LIBRARY",
        "merchant_id": merchant_id,
        "filename": filename,
        "file_base64": base64.b64encode(data).decode("ascii"),
        "media_library_type": "IMAGE",
        "media_library_format": media_format,
        "tags": list(tags or [])[:MAX_TAGS],
    }
    encoded = json.dumps(body)
    if max_body_bytes > 0 and len(encoded.encode("utf-8")) > max_body_bytes:
        raise MediaMigrationError("upload_body_too_large", "上传体超过 AM 网关上限，已跳过该素材")
    try:
        response = client.post("/ajax/media-library/upload", json=body)
    except CONNECTION_ERRORS as exc:
        raise MediaMigrationError(
            "prod_unavailable", f"无法连接 Prod（{exc.__class__.__name__}）", 502
        ) from exc
    _reject_auth(response, "Prod", "prod")
    if response.status_code not in {200, 201}:
        raise MediaMigrationError(
            "prod_upload_failed", f"Prod 上传失败（HTTP {response.status_code}）", 502
        )
    try:
        media = (response.json() or {}).get("media_library") or {}
    except ValueError as exc:
        raise MediaMigrationError("prod_invalid_response", "Prod 上传响应不是有效 JSON", 502) from exc
    media_id = str(media.get("id") or "").strip()
    if not media_id:
        raise MediaMigrationError("prod_invalid_response", "Prod 上传响应缺少 media_library.id", 502)
    return media


def download_image(client: httpx.Client, url: str, settings: Settings) -> bytes:
    target = str(url or "").strip()
    if not target:
        raise MediaMigrationError("source_url_missing", "UAT 素材缺少下载地址")
    parts = urlsplit(target)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise MediaMigrationError("source_url_invalid", "UAT 素材下载地址无效")
    try:
        response = client.get(target)
    except CONNECTION_ERRORS as exc:
        raise MediaMigrationError(
            "download_failed", f"下载失败（{exc.__class__.__name__}）", 502
        ) from exc
    if response.status_code >= 400:
        raise MediaMigrationError("download_failed", f"下载失败（HTTP {response.status_code}）", 502)
    data = response.content
    if not data:
        raise MediaMigrationError("download_failed", "下载到的图片为空")
    if len(data) > settings.max_download_bytes:
        raise MediaMigrationError("download_too_large", "图片超过下载体积上限")
    return data


# --- job records ----------------------------------------------------------


def _job_doc(job: dict[str, Any]) -> dict[str, Any]:
    """The persisted shape. Sessions and the cancel flag are deliberately absent."""
    spec: MigrationSpec = job["_spec"]
    return {
        "status": job["status"],
        **spec.scope,
        "skip_same_name": spec.skip_same_name,
        "force": spec.force,
        "limit": spec.limit,
        "counters": dict(job["counters"]),
        "current": job["current"],
        "error": job["error"],
        "items": [_public(item) for item in job["items"]],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
    }


def _save_job(db, job: dict[str, Any]) -> None:
    try:
        db[JOB_COLLECTION].update_one(
            {"_id": job["job_id"]}, {"$set": _job_doc(job)}, upsert=True
        )
    except PyMongoError as exc:
        raise _mongo_error(exc) from exc


def serialize_job(document: dict[str, Any]) -> dict[str, Any]:
    """API shape of one stored job, with datetimes as UTC ISO strings."""
    return {
        "job_id": str(document.get("_id") or document.get("job_id") or ""),
        "status": str(document.get("status") or ""),
        "uat_base_url": str(document.get("uat_base_url") or ""),
        "uat_merchant_id": str(document.get("uat_merchant_id") or ""),
        "prod_base_url": str(document.get("prod_base_url") or ""),
        "prod_merchant_id": str(document.get("prod_merchant_id") or ""),
        "skip_same_name": bool(document.get("skip_same_name")),
        "force": bool(document.get("force")),
        "limit": int(document.get("limit") or 0),
        "counters": dict(document.get("counters") or {}),
        "current": str(document.get("current") or ""),
        "error": document.get("error"),
        "items": [_public(item) for item in document.get("items") or []],
        "created_at": utc_iso(document.get("created_at")),
        "started_at": utc_iso(document.get("started_at")),
        "finished_at": utc_iso(document.get("finished_at")),
    }


def serialize_job_summary(document: dict[str, Any]) -> dict[str, Any]:
    summary = serialize_job(document)
    summary.pop("items", None)
    summary.pop("current", None)
    return summary


def mark_interrupted(db) -> int:
    """Release jobs whose worker died with the previous process; the operator restarts them."""
    jobs = list(db[JOB_COLLECTION].find({"status": {"$in": list(ACTIVE_STATUSES)}}))
    for document in jobs:
        uploaded = int((document.get("counters") or {}).get("uploaded") or 0)
        db[JOB_COLLECTION].update_one(
            {"_id": document["_id"], "status": {"$in": list(ACTIVE_STATUSES)}},
            {
                "$set": {
                    "status": "partial" if uploaded else "failed",
                    "current": "",
                    "error": INTERRUPTED_MESSAGE,
                    "finished_at": now(),
                }
            },
        )
    return len(jobs)


# --- job runner -----------------------------------------------------------


def start_job(
    settings: Settings,
    db,
    payload: dict[str, Any],
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    spec = MigrationSpec(payload)
    with _jobs_lock:
        try:
            active = db[JOB_COLLECTION].find_one(
                {"status": {"$in": list(ACTIVE_STATUSES)}, **spec.scope}
            )
        except PyMongoError as exc:
            raise _mongo_error(exc) from exc
        if active is not None:
            return serialize_job(active)
        job: dict[str, Any] = {
            "job_id": f"mmig_{uuid.uuid4().hex[:12]}",
            "status": "queued",
            "counters": {
                "uat_total": 0,
                "prod_total": 0,
                "planned": 0,
                "skipped_ledger": 0,
                "skipped_same_name": 0,
                "uploaded": 0,
                "compressed": 0,
                "failed": 0,
                "done": 0,
            },
            "current": "",
            "error": None,
            "items": [],
            "created_at": now(),
            "started_at": None,
            "finished_at": None,
            "cancel_requested": False,
            "_spec": spec,
        }
        _jobs[job["job_id"]] = job
        _save_job(db, job)
    threading.Thread(
        target=_guarded_run,
        args=(settings, db, job, transport),
        name=f"media-migration-{job['job_id']}",
        daemon=True,
    ).start()
    return get_job(db, job["job_id"])


def get_job(db, job_id: str) -> dict[str, Any]:
    """Read a job from MongoDB, so a run started by a previous process stays inspectable."""
    try:
        document = db[JOB_COLLECTION].find_one({"_id": str(job_id or "").strip()})
    except PyMongoError as exc:
        raise _mongo_error(exc) from exc
    if document is None:
        raise MediaMigrationError("job_not_found", "迁移任务不存在", 404)
    return serialize_job(document)


def list_jobs(
    db,
    *,
    uat_merchant_id: str = "",
    prod_merchant_id: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if uat_merchant_id.strip():
        query["uat_merchant_id"] = uat_merchant_id.strip()
    if prod_merchant_id.strip():
        query["prod_merchant_id"] = prod_merchant_id.strip()
    bounded = max(1, min(int(limit or 20), 100))
    try:
        documents = list(
            db[JOB_COLLECTION].find(query).sort("created_at", -1).limit(bounded)
        )
    except PyMongoError as exc:
        raise _mongo_error(exc) from exc
    return {"jobs": [serialize_job_summary(document) for document in documents]}


def cancel_job(db, job_id: str) -> dict[str, Any]:
    job = _jobs.get(str(job_id or "").strip())
    if job is not None and job["status"] in ACTIVE_STATUSES:
        job["cancel_requested"] = True
    return get_job(db, job_id)


def reset_jobs() -> None:
    """Test hook: drop the in-memory coordination state between cases."""
    with _jobs_lock:
        _jobs.clear()


def _guarded_run(
    settings: Settings, db, job: dict[str, Any], transport: httpx.BaseTransport | None
) -> None:
    try:
        run_job(settings, db, job, transport=transport)
    except MediaMigrationError as exc:
        job["status"] = "failed"
        job["error"] = exc.message
    except Exception as exc:  # noqa: BLE001 - the polled job must show the failure
        logger.exception("media migration job failed: %s", job["job_id"])
        job["status"] = "failed"
        job["error"] = f"迁移中断：{exc.__class__.__name__}"
    finally:
        job["current"] = ""
        job["finished_at"] = now()
        _save_job(db, job)


def run_job(
    settings: Settings,
    db,
    job: dict[str, Any],
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    spec: MigrationSpec = job["_spec"]
    job["status"] = "running"
    job["started_at"] = now()
    _save_job(db, job)
    planned_state = plan(settings, db, spec, transport=transport)
    items = planned_state["_items"]
    planned = [item for item in items if item["status"] == "planned"]
    if spec.limit:
        # A partial run leaves the rest visibly planned, so the next run picks them up.
        planned = planned[: spec.limit]
    job["items"] = items
    job["counters"].update(
        {
            "uat_total": planned_state["uat_total"],
            "prod_total": planned_state["prod_total"],
            "planned": len(planned),
            "skipped_ledger": planned_state["skipped_ledger"],
            "skipped_same_name": planned_state["skipped_same_name"],
        }
    )
    _save_job(db, job)
    counters = job["counters"]
    fatal: MediaMigrationError | None = None
    max_bytes = upload_body_max_bytes(settings)
    with download_client(transport) as downloads, am_client(
        spec.prod_base_url, spec.prod_cookie, transport
    ) as prod:
        for item in planned:
            if job["cancel_requested"]:
                job["status"] = "cancelled"
                break
            job["current"] = item["name"]
            try:
                data = download_image(downloads, item.get("_url") or "", settings)
                source_sha = hashlib.sha256(data).hexdigest()
                data, media_format, filename = prepare_upload(data, item, settings)
                media = upload_image(
                    prod,
                    spec.prod_merchant_id,
                    filename=filename,
                    data=data,
                    media_format=media_format,
                    tags=item.get("tags") or [],
                    max_body_bytes=int(settings.am_upload_max_body_bytes),
                )
                compressed = media_format != str(item.get("format") or "").upper()
                item["status"] = "uploaded"
                item["prod_media_id"] = str(media.get("id") or "")
                item["error"] = None
                item["reason"] = ""
                item["format"] = media_format
                counters["uploaded"] += 1
                if compressed:
                    counters["compressed"] += 1
                # Flush per item: a restart must not re-upload what Prod already has.
                record_item(
                    db,
                    spec,
                    item,
                    status="uploaded",
                    job_id=job["job_id"],
                    prod_media_id=item["prod_media_id"],
                    uploaded_filename=filename,
                    sha256=source_sha,
                    size=len(data),
                    compressed=compressed,
                )
            except MediaMigrationError as exc:
                item["status"] = "failed"
                item["error"] = exc.message
                counters["failed"] += 1
                record_item(
                    db, spec, item, status="failed", job_id=job["job_id"], error=exc.message
                )
                if exc.code in AUTH_CODES:
                    fatal = exc
            except Exception as exc:  # noqa: BLE001 - report per item and continue
                message = f"{exc.__class__.__name__}: {exc}"[:300]
                item["status"] = "failed"
                item["error"] = message
                counters["failed"] += 1
                record_item(db, spec, item, status="failed", job_id=job["job_id"], error=message)
            finally:
                counters["done"] = counters["uploaded"] + counters["failed"]
                _save_job(db, job)
            if fatal is not None:
                break
    if fatal is not None:
        job["status"] = "failed"
        job["error"] = fatal.message
    elif job["status"] != "cancelled":
        if counters["failed"] == 0:
            job["status"] = "succeeded"
        elif counters["uploaded"] > 0:
            job["status"] = "partial"
        else:
            job["status"] = "failed"
        job["error"] = None if counters["failed"] == 0 else f"{counters['failed']} 个素材迁移失败"
    _save_job(db, job)
    return get_job(db, job["job_id"])
