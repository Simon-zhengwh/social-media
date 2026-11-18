from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.api_paths import original_replacement_download_url
from app.request_context import request_db
from app.routes import whole_image as whole_routes

router = APIRouter(prefix="/task-center", tags=["task-center"])

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


def _workspace_replacements(
    db: Any, workspace_by_candidate: dict[str, str], status: str,
) -> dict[str, dict[str, Any]]:
    replacements: dict[str, dict[str, Any]] = {}
    if not workspace_by_candidate:
        return replacements
    docs = db.background_replace_tasks.find(
        {"whole_task_id": {"$type": "string", "$ne": ""}},
        {"whole_task_id": 1, "merchant_id": 1, "status": 1, "created_at": 1, "output.asset_id": 1},
    ).sort([("created_at", -1), ("_id", -1)])
    for doc in docs:
        candidate_id = doc["whole_task_id"]
        workspace_id = workspace_by_candidate.get(candidate_id)
        if workspace_id is None:
            continue
        run_status = doc.get("status")
        stage = "active" if run_status in {"queued", "running"} else "ready" if run_status == "succeeded" else "failed"
        if status != "all" and stage != status:
            continue
        previous = replacements.get(workspace_id)
        if previous and (previous["status"] == "active" or stage != "active"):
            continue
        replacement_id = str(doc["_id"])
        replacements[workspace_id] = {
            "id": replacement_id,
            "whole_task_id": candidate_id,
            "status": stage,
            "download_url": original_replacement_download_url(str(doc.get("merchant_id") or ""), candidate_id, replacement_id)
            if (doc.get("output") or {}).get("asset_id") else None,
        }
    return replacements


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

    db = request_db(request)
    groups = whole_routes._list_task_groups(db)
    workspace_by_candidate = {candidate["id"]: group["id"] for group in groups for candidate in group["candidates"]}
    replacements = _workspace_replacements(db, workspace_by_candidate, status if kind == "original_replace" else "all")
    items: list[dict[str, Any]] = []
    for group in groups:
        replacement = replacements.get(group["id"])
        if kind == "original_replace" and replacement is None:
            continue
        group["kind"] = "generate"
        group["original_replacement"] = replacement
        if replacement and replacement["status"] == "active":
            group["status"] = "active"
        items.append(group)

    if status != "all" and kind != "original_replace":
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
