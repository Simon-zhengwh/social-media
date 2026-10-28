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
KINDS = {"all", "generate", "edit"}


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)


def _poster_candidate(db: Any, task: dict[str, Any]) -> dict[str, Any]:
    outputs = [item for item in task.get("outputs") or [] if isinstance(item, dict)]
    final_output = next(
        (item for item in outputs if str(item.get("role") or "").startswith("agent_result")),
        None,
    )
    preview_output = final_output or (outputs[0] if outputs else None)
    task_id = str(task["_id"])
    output_id = str((preview_output or {}).get("id") or "")
    asset_id = str((preview_output or {}).get("asset_id") or "")
    status = str(task.get("status") or "failed")
    error = task.get("error") if isinstance(task.get("error"), dict) else None
    return {
        "id": task_id,
        "candidate_index": int(task.get("candidate_index") or 0),
        "status": status,
        "stage": str(task.get("stage") or "failed"),
        "progress": int(task.get("progress") or 0),
        "current_version": 0,
        "preview_url": f"/api/assets/{asset_id}/file" if asset_id else None,
        "download_url": (
            f"/api/poster-edits/{task_id}/download?output_id={output_id}"
            if asset_id and output_id
            else None
        ),
        "edit_status": "idle",
        "error": str((error or {}).get("message") or "") or None,
        "created_at": utc_iso(task.get("created_at")),
        "updated_at": utc_iso(task.get("updated_at")),
    }


def _poster_group(db: Any, batch_id: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(
        tasks,
        key=lambda item: (
            int(item.get("candidate_index") or 0),
            _as_utc(item.get("created_at")),
            str(item.get("_id")),
        ),
    )
    candidates = [_poster_candidate(db, item) for item in ordered]
    active_count = sum(
        1 for item in ordered if item.get("status") in {"queued", "running"}
    )
    succeeded_count = sum(
        1 for item in ordered if item.get("status") == "succeeded"
    )
    failed_count = sum(
        1 for item in ordered if item.get("status") in {"failed", "interrupted", "partial"}
    )
    if active_count:
        status = "active"
    elif succeeded_count:
        status = "ready"
    else:
        status = "failed"
    representative = next(
        (item for item in candidates if item["status"] == "succeeded"),
        next((item for item in candidates if item["status"] in {"queued", "running"}), candidates[0]),
    )
    first = ordered[0]
    first_input = first.get("input") if isinstance(first.get("input"), dict) else {}
    first_asset_id = next(iter(first_input.get("asset_ids") or []), None)
    first_asset = db.assets.find_one({"_id": first_asset_id}) if first_asset_id else None
    error_message = next((item["error"] for item in candidates if item["error"]), None)
    return {
        "id": batch_id,
        "kind": "edit",
        "status": status,
        "prompt": str(first_input.get("prompt") or ""),
        "brand_name": None,
        "platform_name": None,
        "placement_name": None,
        "canvas": {
            "width": int((first_asset or {}).get("width") or 0),
            "height": int((first_asset or {}).get("height") or 0),
        },
        "candidate_count": len(candidates),
        "active_count": active_count,
        "succeeded_count": succeeded_count,
        "failed_count": failed_count,
        "retryable_count": failed_count,
        "representative_task_id": representative["id"],
        "preview_url": representative["preview_url"],
        "download_url": representative["download_url"],
        "error_message": error_message,
        "candidates": candidates,
        "created_at": min(_as_utc(item.get("created_at")) for item in ordered),
        "updated_at": max(_as_utc(item.get("updated_at")) for item in ordered),
    }


def _poster_groups(db: Any) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for task in db.poster_edit_tasks.find({}).sort("created_at", -1):
        batch_id = str(task.get("batch_id") or task["_id"])
        grouped.setdefault(batch_id, []).append(task)
    return [_poster_group(db, batch_id, tasks) for batch_id, tasks in grouped.items()]


def _encode_cursor(item: dict[str, Any]) -> str:
    payload = json.dumps(
        {"created_at": _as_utc(item["created_at"]).isoformat(), "id": item["id"]},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


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
        raise HTTPException(status_code=422, detail="kind 只支持 all、generate 或 edit")
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail="limit 需在 1 到 100 之间")

    db = request.app.state.db
    items: list[dict[str, Any]] = []
    if kind in {"all", "generate"}:
        whole_docs = list(db.whole_tasks.find({}).sort("created_at", -1))
        for group in whole_routes._build_task_groups(whole_docs):
            group["kind"] = "generate"
            items.append(group)
    if kind in {"all", "edit"}:
        items.extend(_poster_groups(db))

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
