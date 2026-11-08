from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from pymongo import ReturnDocument

CONTROL_ID = "maintenance"
WRITE_BLOCKED_MESSAGE = "系统正在维护，暂不接受新的写入或付费操作，请稍后重试。"


class MaintenanceError(Exception):
    pass


def initialize(db: Any) -> None:
    now = datetime.now(timezone.utc)
    db.app_control.update_one(
        {"_id": CONTROL_ID},
        {
            "$setOnInsert": {
                "enabled": False,
                "created_at": now,
            },
            "$set": {"admitting": 0, "updated_at": now},
        },
        upsert=True,
    )


@contextmanager
def admit(db: Any) -> Iterator[None]:
    control = db.app_control.find_one_and_update(
        {"_id": CONTROL_ID, "enabled": False},
        {"$inc": {"admitting": 1}, "$set": {"updated_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )
    if control is None:
        raise MaintenanceError(WRITE_BLOCKED_MESSAGE)
    try:
        yield
    finally:
        db.app_control.update_one(
            {"_id": CONTROL_ID},
            {"$inc": {"admitting": -1}, "$set": {"updated_at": datetime.now(timezone.utc)}},
        )


def enable(db: Any) -> dict[str, Any]:
    db.app_control.update_one(
        {"_id": CONTROL_ID},
        {"$set": {"enabled": True, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return status(db)


def disable(db: Any) -> dict[str, Any]:
    db.app_control.update_one(
        {"_id": CONTROL_ID},
        {"$set": {"enabled": False, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return status(db)


def status(db: Any) -> dict[str, Any]:
    control = db.app_control.find_one({"_id": CONTROL_ID}) or {}
    counts = {
        "whole_tasks": db.whole_tasks.count_documents({"status": {"$in": ["queued", "running"]}}),
        "whole_edits": db.whole_tasks.count_documents({"edit.status": "running"}),
        "poster_edits": db.poster_edit_tasks.count_documents({"status": {"$in": ["queued", "running"]}}),
        "background_replacements": db.background_replace_tasks.count_documents({"status": {"$in": ["queued", "running"]}}),
        "agent_tests": db.agent_test_runs.count_documents({"status": {"$in": ["queued", "preparing", "triggering", "running"]}}),
    }
    active = sum(counts.values())
    admitting = int(control.get("admitting") or 0)
    return {
        "enabled": bool(control.get("enabled")),
        "admitting": admitting,
        "active_tasks": counts,
        "drained": admitting == 0 and active == 0,
    }


def wait_until_drained(db: Any, *, timeout_seconds: int = 0, poll_seconds: float = 2.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds if timeout_seconds > 0 else None
    while True:
        current = status(db)
        if current["drained"]:
            return current
        if deadline is not None and time.monotonic() >= deadline:
            current["timed_out"] = True
            return current
        time.sleep(poll_seconds)
