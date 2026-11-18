"""Merchant-scoped persistence for the asset search index.

Three collections keep the library searchable without calling a model on every
keystroke:

- ``asset_search_records`` mirrors searchable names, tags and bilingual keywords.
- ``asset_index_runs`` records one background pass over a fixed list of asset ids.
- ``asset_search_queries`` stores the vocabulary a semantic search produced, so paging
  never calls the model again.

Every helper takes the scoped database, so a background run cannot cross merchants.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app.services import asset_records

RECORD_COLLECTION = "asset_search_records"
RUN_COLLECTION = "asset_index_runs"
QUERY_COLLECTION = "asset_search_queries"

# Keyword lifecycle; persisted status/version names remain compatible with older runs.
PENDING = "pending"
RUNNING = "running"
READY = "ready"
FAILED = "failed"

# `initialize` is the one-off bootstrap that also writes names and tags. Everything the
# operator can trigger later only refreshes keywords; `retry` re-queues unfinished rows.
RUN_INITIALIZE = "initialize"
RUN_REINDEX = "reindex"
RUN_RETRY = "retry"
RUN_DESCRIBE = "describe"
RUN_MODES = (RUN_INITIALIZE, RUN_REINDEX, RUN_RETRY, RUN_DESCRIBE)

ACTIVE_RUN_STATUSES = ("queued", "running")
FINISHED_RUN_STATUSES = ("succeeded", "interrupted", "failed")
MAX_RUN_ERRORS = 60
QUERY_TTL_SECONDS = 1800


class AssetIndexError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _aware(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def catalog_entry(asset: dict[str, Any]) -> dict[str, Any]:
    """The searchable slice of one asset document."""
    return {
        "asset_id": str(asset["_id"]),
        "name": str(asset.get("original_name") or ""),
        "tags": [str(tag) for tag in asset.get("tags") or [] if str(tag).strip()],
        "source": str(asset.get("source") or "upload"),
        "created_at": _aware(asset.get("created_at")),
        "url": str(asset.get("prepared_url") or asset.get("url") or ""),
        "mime_type": str(asset.get("mime_type") or "image/png"),
        "asset_marker": asset_marker(asset),
    }


def asset_marker(asset: dict[str, Any]) -> datetime | None:
    """Latest metadata edit, used to avoid overwriting a concurrent rename or tag edit."""
    candidates = [
        _aware(asset.get(key))
        for key in ("name_updated_at", "tags_updated_at", "created_at")
    ]
    values = [value for value in candidates if value is not None]
    return max(values) if values else None


def local_catalog(db: Any) -> list[dict[str, Any]]:
    """Every asset of the merchant that belongs in the library, newest first."""
    query = {**asset_records.uploaded_asset_query(), "am_media_library_id": {"$exists": False}}
    return [catalog_entry(doc) for doc in db.assets.find(query)]


def all_catalog(db: Any) -> list[dict[str, Any]]:
    """Library rows regardless of their AM mapping: the search index counts them all."""
    return [catalog_entry(doc) for doc in db.assets.find(asset_records.uploaded_asset_query())]


def record_for_asset(db: Any, asset_id: str) -> dict[str, Any] | None:
    return db[RECORD_COLLECTION].find_one({"_id": asset_id})


def sync_catalog(db: Any, entries: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Mirror names, tags and ordering into the records, keeping existing keywords.

    Stale records are only removed once a full catalog has been read successfully, so a
    failed sync can never empty the index.
    """
    kept: list[str] = []
    added = updated = 0
    stamp = now()
    for entry in entries:
        asset_id = entry["asset_id"]
        kept.append(asset_id)
        existing = db[RECORD_COLLECTION].find_one({"_id": asset_id})
        mirror = {
            "asset_id": asset_id,
            "name": entry["name"],
            "tags": list(entry["tags"]),
            "source": entry["source"],
            "created_at": entry.get("created_at"),
            "url": entry["url"],
            "mime_type": entry["mime_type"],
            "asset_marker": entry.get("asset_marker"),
            "catalog_synced_at": stamp,
        }
        if existing is None:
            db[RECORD_COLLECTION].insert_one(
                {
                    "_id": asset_id,
                    **mirror,
                    "keywords": {"zh": [], "en": []},
                    "description_status": PENDING,
                    "description_error": "",
                    "description_version": 0,
                    "description_updated_at": None,
                    "claim_run_id": None,
                    "created_record_at": stamp,
                }
            )
            added += 1
            continue
        db[RECORD_COLLECTION].update_one({"_id": asset_id}, {"$set": mirror})
        updated += 1
    removed = 0
    if kept:
        removed = db[RECORD_COLLECTION].delete_many({"_id": {"$nin": kept}}).deleted_count
    return {"added": added, "updated": updated, "removed": removed, "total": len(kept)}


def delete_record(db: Any, asset_id: str) -> None:
    db[RECORD_COLLECTION].delete_one({"_id": asset_id})


def deleted_record_ids(db: Any, asset_ids: Iterable[str]) -> int:
    ids = [str(item) for item in asset_ids if str(item)]
    if not ids:
        return 0
    return db[RECORD_COLLECTION].delete_many({"_id": {"$in": ids}}).deleted_count


def request_description(db: Any, asset: dict[str, Any]) -> dict[str, Any]:
    """Queue keyword regeneration and bump the version older results must not beat."""
    entry = catalog_entry(asset)
    stamp = now()
    record = db[RECORD_COLLECTION].find_one_and_update(
        {"_id": entry["asset_id"]},
        {
            "$set": {
                "asset_id": entry["asset_id"],
                "name": entry["name"],
                "tags": entry["tags"],
                "source": entry["source"],
                "created_at": entry.get("created_at"),
                "url": entry["url"],
                "mime_type": entry["mime_type"],
                "asset_marker": entry.get("asset_marker"),
                "description_status": PENDING,
                "description_error": "",
            },
            "$inc": {"description_version": 1},
            "$setOnInsert": {
                "keywords": {"zh": [], "en": []},
                "description_updated_at": None,
                "created_record_at": stamp,
            },
        },
        upsert=True,
        return_document=True,
    )
    return record


def claim_record(db: Any, asset_id: str, run_id: str) -> dict[str, Any] | None:
    """Claim one record for this run; another live run cannot take it at the same time."""
    return db[RECORD_COLLECTION].find_one_and_update(
        {"_id": asset_id, "claim_run_id": {"$in": [None, run_id]}},
        {
            "$set": {
                "claim_run_id": run_id,
                "description_status": RUNNING,
                "description_started_at": now(),
            }
        },
        return_document=False,
    )


def release_record(db: Any, asset_id: str, run_id: str) -> None:
    db[RECORD_COLLECTION].update_one(
        {"_id": asset_id, "claim_run_id": run_id}, {"$set": {"claim_run_id": None}}
    )


def save_keywords(
    db: Any,
    asset_id: str,
    *,
    version: int,
    keywords: dict[str, list[str]],
) -> bool:
    """Apply keywords only while this request is still newest."""
    values = {
        "keywords": keywords,
        "description_status": READY,
        "description_error": "",
        "description_updated_at": now(),
        "claim_run_id": None,
    }
    result = db[RECORD_COLLECTION].update_one(
        {"_id": asset_id, "description_version": version},
        {"$set": values},
    )
    return bool(result.matched_count)


def fail_record(
    db: Any, asset_id: str, *, version: int | None, error: str, keep_keywords: bool = True
) -> None:
    query: dict[str, Any] = {"_id": asset_id}
    if version is not None:
        query["description_version"] = version
    update: dict[str, Any] = {
        "$set": {
            "description_status": FAILED,
            "description_error": error[:300],
            "claim_run_id": None,
        }
    }
    if not keep_keywords:
        update["$set"]["keywords"] = {"zh": [], "en": []}
    db[RECORD_COLLECTION].update_one(query, update)


def mark_name_tags(
    db: Any, asset_id: str, *, name: str, tags: list[str], asset_marker: datetime | None
) -> None:
    db[RECORD_COLLECTION].update_one(
        {"_id": asset_id},
        {"$set": {"name": name, "tags": list(tags), "asset_marker": asset_marker}},
    )


def empty_counters(total: int = 0) -> dict[str, int]:
    return {"total": total, "succeeded": 0, "failed": 0, "skipped": 0, "remaining": total}


def create_run(
    db: Any,
    *,
    mode: str,
    asset_ids: list[str],
    request_id: str = "",
    reason: str = "",
) -> tuple[dict[str, Any], bool]:
    """Create a run, or return the active/newest matching one instead of duplicating work."""
    if mode not in RUN_MODES:
        raise AssetIndexError("invalid_mode", "未知的索引任务类型")
    key = request_id.strip() or new_id("req")
    existing = db[RUN_COLLECTION].find_one({"request_id": key})
    if existing is not None:
        return existing, False
    active = db[RUN_COLLECTION].find_one(
        {"status": {"$in": list(ACTIVE_RUN_STATUSES)}}, sort=[("created_at", -1)]
    )
    if active is not None:
        # A second click (or an initialization still running) must join the live task.
        return active, False
    document = {
        "_id": new_id("arun"),
        "mode": mode,
        "status": "queued",
        "scope": list(dict.fromkeys(asset_ids)),
        "request_id": key,
        "reason": reason,
        "counters": empty_counters(len(set(asset_ids))),
        "errors": [],
        "error": None,
        "created_at": now(),
        "started_at": None,
        "finished_at": None,
    }
    db[RUN_COLLECTION].insert_one(document)
    return db[RUN_COLLECTION].find_one({"_id": document["_id"]}), True


def get_run(db: Any, run_id: str) -> dict[str, Any]:
    run = db[RUN_COLLECTION].find_one({"_id": run_id})
    if run is None:
        raise AssetIndexError("run_not_found", "索引任务不存在", 404)
    return run


def set_counters(db: Any, run_id: str, counters: dict[str, int]) -> None:
    db[RUN_COLLECTION].update_one({"_id": run_id}, {"$set": {"counters": counters}})


def record_run_error(db: Any, run_id: str, asset_id: str, message: str) -> None:
    db[RUN_COLLECTION].update_one(
        {"_id": run_id},
        {
            "$push": {
                "errors": {
                    "$each": [{"asset_id": asset_id, "message": message[:300], "at": now()}],
                    "$slice": -MAX_RUN_ERRORS,
                }
            }
        },
    )


def finish_run(
    db: Any, run_id: str, *, status: str, counters: dict[str, int], error: str | None = None
) -> None:
    db[RUN_COLLECTION].update_one(
        {"_id": run_id},
        {
            "$set": {
                "status": status,
                "counters": counters,
                "error": error,
                "finished_at": now(),
            }
        },
    )


def mark_started(db: Any, run_id: str) -> None:
    run = db[RUN_COLLECTION].find_one({"_id": run_id}) or {}
    db[RUN_COLLECTION].update_one(
        {"_id": run_id},
        {"$set": {"status": "running", "started_at": run.get("started_at") or now()}},
    )


def interrupt_active_runs(db: Any) -> list[str]:
    """Release a crashed process: its runs are interrupted and its claims dropped."""
    interrupted: list[str] = []
    for run in db[RUN_COLLECTION].find({"status": {"$in": list(ACTIVE_RUN_STATUSES)}}):
        db[RUN_COLLECTION].update_one(
            {"_id": run["_id"]},
            {
                "$set": {
                    "status": "interrupted",
                    "error": "索引进程中断，可重新发起",
                    "finished_at": now(),
                }
            },
        )
        db[RECORD_COLLECTION].update_many(
            {"claim_run_id": run["_id"]},
            {"$set": {"claim_run_id": None, "description_status": PENDING}},
        )
        interrupted.append(str(run["_id"]))
    # Claims left by a process that never wrote a run are equally unusable.
    db[RECORD_COLLECTION].update_many(
        {"description_status": RUNNING, "claim_run_id": None},
        {"$set": {"description_status": PENDING}},
    )
    return interrupted


def summary(db: Any) -> dict[str, Any]:
    total = db[RECORD_COLLECTION].count_documents({})
    ready = db[RECORD_COLLECTION].count_documents({"description_status": READY})
    failed = db[RECORD_COLLECTION].count_documents({"description_status": FAILED})
    running = db[RECORD_COLLECTION].count_documents({"description_status": RUNNING})
    pending = db[RECORD_COLLECTION].count_documents(
        {"description_status": {"$in": [PENDING, None]}}
    )
    active = db[RUN_COLLECTION].find_one(
        {"status": {"$in": list(ACTIVE_RUN_STATUSES)}}, sort=[("created_at", -1)]
    )
    latest = db[RUN_COLLECTION].find_one({}, sort=[("created_at", -1)])
    return {
        "total": total,
        "described": ready,
        "failed": failed,
        "running": running,
        "pending": pending,
        "active_run": run_out(active) if active else None,
        "last_run": run_out(latest) if latest else None,
    }


def run_out(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": run["_id"],
        "mode": run.get("mode"),
        "status": run.get("status"),
        "counters": run.get("counters") or {},
        "errors": list(run.get("errors") or [])[-MAX_RUN_ERRORS:],
        "error": run.get("error"),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
    }


def unfinished_asset_ids(db: Any) -> list[str]:
    return [
        str(record["_id"])
        for record in db[RECORD_COLLECTION].find(
            {"description_status": {"$ne": READY}}, {"_id": 1}
        )
    ]


def save_query(
    db: Any,
    *,
    query: str,
    mode: str,
    groups: list[dict[str, Any]],
    degraded: bool = False,
    degraded_reason: str | None = None,
    model: str = "",
    prompt_version: str = "",
) -> dict[str, Any]:
    stamp = now()
    document = {
        "_id": new_id("asearch"),
        "query": query,
        "mode": mode,
        "groups": groups,
        "degraded": degraded,
        "degraded_reason": degraded_reason,
        "model": model,
        "prompt_version": prompt_version,
        "created_at": stamp,
        "expires_at": stamp + timedelta(seconds=QUERY_TTL_SECONDS),
    }
    db[QUERY_COLLECTION].insert_one(document)
    # Expired vocabularies are useless and paging never refreshes them; drop them on write so
    # a long session of typing does not grow the collection without bound.
    db[QUERY_COLLECTION].delete_many({"expires_at": {"$lt": stamp}})
    return document


def get_query(db: Any, search_id: str) -> dict[str, Any]:
    document = db[QUERY_COLLECTION].find_one({"_id": search_id})
    if document is None:
        raise AssetIndexError("search_not_found", "搜索已失效，请重新提交", 404)
    expires_at = _aware(document.get("expires_at"))
    if expires_at is not None and expires_at < now():
        # Paging must never call the model behind the operator's back.
        raise AssetIndexError("search_expired", "搜索已过期，请重新提交", 410)
    return document


def query_out(document: dict[str, Any], *, total: int) -> dict[str, Any]:
    return {
        "id": document["_id"],
        "query": document.get("query") or "",
        "mode": document.get("mode") or "literal",
        "degraded": bool(document.get("degraded")),
        "degraded_reason": document.get("degraded_reason"),
        "groups": list(document.get("groups") or []),
        "model": document.get("model") or "",
        "total": total,
        "expires_at": document.get("expires_at"),
    }
