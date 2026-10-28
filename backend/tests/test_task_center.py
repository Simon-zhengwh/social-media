from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import mongomock
import pytest
from fastapi import BackgroundTasks, HTTPException

from app.routes import task_center as task_center_routes
from app.routes import whole_image as whole_routes


BASE_TIME = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)


def _task(
    task_id: str,
    *,
    batch_id: str | None = None,
    candidate_index: int = 0,
    status: str = "succeeded",
    created_at: datetime = BASE_TIME,
    prompt: str = "夏日冰美式海报",
    brand_name: str = "SEEJOY 喜聚",
    output: bool = False,
    edit_status: str = "idle",
    edit_error: str | None = None,
    error: str | None = None,
) -> dict:
    has_output = output or status == "succeeded"
    return {
        "_id": task_id,
        "status": status,
        "stage": "done" if status == "succeeded" else "failed",
        "progress": 100 if status == "succeeded" else 0,
        "input": {
            "prompt": prompt,
            "candidate_batch_id": batch_id,
            "candidate_index": candidate_index,
        },
        "brand": {"name": brand_name},
        "platform": {"name": "微信朋友圈"},
        "placement": {"name": "单图"},
        "canvas": {"width": 1080, "height": 1920},
        "current_version": 1 if has_output else 0,
        "current_version_id": f"ver_{task_id}" if has_output else None,
        "selected_version_id": f"ver_{task_id}" if has_output else None,
        "output_asset_id": f"asset_{task_id}" if has_output else None,
        "generation": {
            "status": "completed" if status == "succeeded" else "failed",
            "error": error,
        },
        "edit": {
            "status": edit_status,
            "instruction": "把标题改成周末特惠" if edit_status == "failed" else None,
            "source_version_id": f"ver_{task_id}" if edit_status == "failed" else None,
            "error": edit_error,
        },
        "error": {"code": "generation_failed", "message": error} if error else None,
        "created_at": created_at,
        "updated_at": created_at,
    }


def _list_request(db):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db=db))
    )


def _retry_request(db):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                db=db,
                settings=SimpleNamespace(core_ai_api_key="test-key"),
                store=object(),
                core_ai=object(),
            )
        )
    )


def test_task_groups_aggregate_batches_filter_and_choose_representative():
    db = mongomock.MongoClient().db
    db.whole_tasks.insert_many(
        [
            _task(
                "wtask_a0",
                batch_id="batch_a",
                candidate_index=0,
                status="failed",
                error="第一张失败",
                created_at=BASE_TIME,
            ),
            _task(
                "wtask_a1",
                batch_id="batch_a",
                candidate_index=1,
                status="succeeded",
                output=True,
                created_at=BASE_TIME + timedelta(seconds=1),
            ),
            _task(
                "wtask_a2",
                batch_id="batch_a",
                candidate_index=2,
                status="succeeded",
                output=True,
                created_at=BASE_TIME + timedelta(seconds=2),
            ),
            _task(
                "wtask_b0",
                batch_id="batch_b",
                candidate_index=0,
                status="queued",
                created_at=BASE_TIME + timedelta(minutes=1),
            ),
            _task(
                "wtask_c0",
                status="failed",
                created_at=BASE_TIME + timedelta(minutes=2),
                error="单任务失败",
            ),
        ]
    )

    result = whole_routes.list_tasks(_list_request(db), "all", 10, None)

    assert result["total"] == 3
    assert [item["id"] for item in result["items"]] == [
        "wtask_c0",
        "batch_b",
        "batch_a",
    ]
    group = result["items"][2]
    assert group["status"] == "ready"
    assert group["candidate_count"] == 3
    assert group["succeeded_count"] == 2
    assert group["failed_count"] == 1
    assert group["retryable_count"] == 1
    assert group["representative_task_id"] == "wtask_a1"
    assert [item["id"] for item in group["candidates"]] == [
        "wtask_a0",
        "wtask_a1",
        "wtask_a2",
    ]
    assert group["preview_url"] == "/api/assets/asset_wtask_a1/file?v=1"

    assert [item["id"] for item in whole_routes.list_tasks(_list_request(db), "active", 10, None)["items"]] == ["batch_b"]
    assert [item["id"] for item in whole_routes.list_tasks(_list_request(db), "ready", 10, None)["items"]] == ["batch_a"]
    assert [item["id"] for item in whole_routes.list_tasks(_list_request(db), "failed", 10, None)["items"]] == ["wtask_c0"]


def test_task_groups_keep_unbatched_tasks_separate():
    db = mongomock.MongoClient().db
    db.whole_tasks.insert_many(
        [
            _task("wtask_one", created_at=BASE_TIME),
            _task("wtask_two", created_at=BASE_TIME + timedelta(seconds=1)),
        ]
    )

    result = whole_routes.list_tasks(_list_request(db), "all", 10, None)

    assert result["total"] == 2
    assert [item["id"] for item in result["items"]] == ["wtask_two", "wtask_one"]


def test_task_group_cursor_pagination_has_no_duplicates():
    db = mongomock.MongoClient().db
    db.whole_tasks.insert_many(
        [
            _task("wtask_0", created_at=BASE_TIME),
            _task("wtask_1", created_at=BASE_TIME + timedelta(seconds=1)),
            _task("wtask_2", created_at=BASE_TIME + timedelta(seconds=2)),
        ]
    )

    first = whole_routes.list_tasks(_list_request(db), "all", 2, None)
    assert [item["id"] for item in first["items"]] == ["wtask_2", "wtask_1"]
    assert first["next_cursor"]

    second = whole_routes.list_tasks(
        _list_request(db), "all", 2, first["next_cursor"]
    )
    assert [item["id"] for item in second["items"]] == ["wtask_0"]
    assert second["next_cursor"] is None


def test_retry_failed_generation_requeues_same_task():
    db = mongomock.MongoClient().db
    db.whole_tasks.insert_one(
        _task("wtask_failed", status="failed", error="模型超时")
    )
    background = BackgroundTasks()

    result = whole_routes.retry_task(
        "wtask_failed", _retry_request(db), background
    )

    assert result["status"] == "queued"
    assert result["stage"] == "pending"
    assert result["error"] is None
    assert len(background.tasks) == 1
    stored = db.whole_tasks.find_one({"_id": "wtask_failed"})
    generation = stored.get("generation") or {}
    provider = stored.get("provider") or {}
    assert stored.get("generation.status") == "queued" or generation.get("status") == "queued"
    assert stored.get("provider.status") == "queued" or provider.get("status") == "queued"


def test_retry_failed_edit_requeues_same_task():
    db = mongomock.MongoClient().db
    db.whole_tasks.insert_one(
        _task(
            "wtask_edit",
            status="succeeded",
            output=True,
            edit_status="failed",
            edit_error="改图超时",
        )
    )
    background = BackgroundTasks()

    result = whole_routes.retry_task("wtask_edit", _retry_request(db), background)

    assert result["status"] == "succeeded"
    assert result["edit"]["status"] == "running"
    assert result["edit"]["error"] is None
    assert len(background.tasks) == 1


@pytest.mark.parametrize(
    "task",
    [
        _task("wtask_running", status="running"),
        _task("wtask_done", status="succeeded", output=True),
    ],
)
def test_retry_rejects_tasks_without_failed_work(task):
    db = mongomock.MongoClient().db
    db.whole_tasks.insert_one(task)

    with pytest.raises(HTTPException) as exc_info:
        whole_routes.retry_task(task["_id"], _retry_request(db), BackgroundTasks())

    assert exc_info.value.status_code == 409


def test_retry_rejects_missing_task():
    db = mongomock.MongoClient().db

    with pytest.raises(HTTPException) as exc_info:
        whole_routes.retry_task("missing", _retry_request(db), BackgroundTasks())

    assert exc_info.value.status_code == 404


def _poster_task(task_id: str, batch_id: str, index: int, status: str, created_at: datetime):
    outputs = []
    if status == "succeeded":
        outputs = [
            {"id": f"out_{task_id}_layer", "role": "agent_transparent", "asset_id": f"asset_{task_id}_layer"},
            {"id": f"out_{task_id}_final", "role": "agent_result", "asset_id": f"asset_{task_id}_final"},
        ]
    return {
        "_id": task_id,
        "batch_id": batch_id,
        "candidate_index": index,
        "candidate_count": 2,
        "status": status,
        "stage": "done" if status == "succeeded" else "failed",
        "progress": 100 if status == "succeeded" else 0,
        "input": {"prompt": "修改海报", "asset_ids": ["asset_source"]},
        "outputs": outputs,
        "error": {"code": "edit_failed", "message": "候选失败"} if status == "failed" else None,
        "created_at": created_at,
        "updated_at": created_at,
    }


def test_unified_task_center_groups_poster_edit_batches_and_filters():
    db = mongomock.MongoClient().db
    db.assets.insert_one({"_id": "asset_source", "width": 1086, "height": 1448})
    db.poster_edit_tasks.insert_many(
        [
            _poster_task("pedit_0", "batch_edit", 0, "succeeded", BASE_TIME),
            _poster_task("pedit_1", "batch_edit", 1, "failed", BASE_TIME + timedelta(seconds=1)),
        ]
    )

    result = task_center_routes.list_task_center(
        _list_request(db), status="all", kind="edit", limit=10, cursor=None
    )
    assert result["total"] == 1
    group = result["items"][0]
    assert group["kind"] == "edit"
    assert group["id"] == "batch_edit"
    assert group["status"] == "ready"
    assert group["candidate_count"] == 2
    assert group["succeeded_count"] == 1
    assert group["failed_count"] == 1
    assert [item["candidate_index"] for item in group["candidates"]] == [0, 1]
    assert group["preview_url"] == "/api/assets/asset_pedit_0_final/file"

    assert task_center_routes.list_task_center(
        _list_request(db), status="all", kind="generate", limit=10, cursor=None
    )["total"] == 0
    assert task_center_routes.list_task_center(
        _list_request(db), status="failed", kind="edit", limit=10, cursor=None
    )["total"] == 0


def test_unified_task_center_cursor_pagination_across_kinds():
    db = mongomock.MongoClient().db
    db.assets.insert_one({"_id": "asset_source", "width": 1086, "height": 1448})
    db.whole_tasks.insert_one(_task("wtask_old", created_at=BASE_TIME))
    db.poster_edit_tasks.insert_one(
        _poster_task("pedit_new", "batch_new", 0, "succeeded", BASE_TIME + timedelta(minutes=1))
    )

    first = task_center_routes.list_task_center(
        _list_request(db), status="all", kind="all", limit=1, cursor=None
    )
    assert [item["id"] for item in first["items"]] == ["batch_new"]
    assert first["next_cursor"]
    second = task_center_routes.list_task_center(
        _list_request(db), status="all", kind="all", limit=1, cursor=first["next_cursor"]
    )
    assert [item["id"] for item in second["items"]] == ["wtask_old"]
    assert second["next_cursor"] is None
