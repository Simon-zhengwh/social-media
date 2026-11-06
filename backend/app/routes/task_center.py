from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.routes import whole_image as whole_routes
from app.time_utils import utc_iso

router = APIRouter(prefix="/api/task-center", tags=["task-center"])

STATUSES = {"all", "active", "ready", "failed"}
KINDS = {"all", "generate", "original_replace"}


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)


def _encode_cursor(item: dict[str, Any]) -> str:
    payload = json.dumps(
        {"created_at": _as_utc(item["created_at"]).isoformat(), "id": item["id"]},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _original_replacement_groups(db: Any) -> list[dict[str, Any]]:
    from app.services.original_replacement_runner import task_out

    groups = []
    for doc in db.background_replace_tasks.find({"whole_task_id": {"$type": "string", "$ne": ""}}).sort("created_at", -1):
        task = task_out(doc)
        active = task["status"] in {"queued", "running"}
        succeeded = task["status"] == "succeeded"
        output = task.get("output") or {}
        error = (task.get("error") or {}).get("message")
        candidate = {"id": task["id"], "candidate_index": 0, "status": task["status"],
                     "stage": task["stage"], "progress": task["progress"], "current_version": 0,
                     "preview_url": output.get("preview_url"), "download_url": output.get("download_url"),
                     "edit_status": "idle", "error": error, "created_at": task["created_at"], "updated_at": task["updated_at"]}
        groups.append({"id": task["id"], "kind": "original_replace", "whole_task_id": task.get("whole_task_id"),
                       "status": "active" if active else "ready" if succeeded else "failed",
                       "prompt": "；".join(item["target"] for item in task["replacements"]),
                       "brand_name": None, "platform_name": None, "placement_name": None,
                       "canvas": {"width": task["source"]["width"], "height": task["source"]["height"]},
                       "candidate_count": 1, "active_count": int(active), "succeeded_count": int(succeeded),
                       "failed_count": int(not active and not succeeded), "retryable_count": 0,
                       "representative_task_id": task["id"], "preview_url": output.get("preview_url"),
                       "download_url": output.get("download_url"), "error_message": error, "candidates": [candidate],
                       "created_at": doc["created_at"], "updated_at": doc["updated_at"]})
    return groups


def _decode_cursor(value: str) -> tuple[datetime, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        return _as_utc(datetime.fromisoformat(str(payload["created_at"]))), str(payload["id"])
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="任务游标无效") from exc


@router.get("")
def list_task_center(
    request: Request,
    status: str = "all",
    kind: str = "all",
    limit: int = 24,
    cursor: str | None = None,
) -> dict[str, Any]:
    if status not in STATUSES:
        raise HTTPException(status_code=422, detail="status 只支持 all、active、ready 或 failed")
    if kind not in KINDS:
        raise HTTPException(status_code=422, detail="kind 只支持 all、generate 或 original_replace")
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail="limit 需在 1 到 100 之间")

    db = request.app.state.db
    items: list[dict[str, Any]] = []
    if kind in {"all", "generate"}:
        whole_docs = list(db.whole_tasks.find({}).sort("created_at", -1))
        for group in whole_routes._build_task_groups(whole_docs):
            group["kind"] = "generate"
            items.append(group)
    if kind in {"all", "original_replace"}:
        items.extend(_original_replacement_groups(db))

    if status != "all":
        items = [item for item in items if item["status"] == status]
    items.sort(key=lambda item: (_as_utc(item["created_at"]), item["id"]), reverse=True)
    total = len(items)

    if cursor:
        cursor_created_at, cursor_id = _decode_cursor(cursor)
        items = [
            item for item in items
            if _as_utc(item["created_at"]) < cursor_created_at
            or (_as_utc(item["created_at"]) == cursor_created_at and item["id"] < cursor_id)
        ]

    page = items[:limit]
    next_cursor = _encode_cursor(page[-1]) if page and len(items) > len(page) else None
    return {"items": page, "next_cursor": next_cursor, "total": total}
