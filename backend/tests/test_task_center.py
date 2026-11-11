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
    workspace_id: str | None = None,
    image_model: str = "gpt-image-2.5-sunburst",
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
            "generation_workspace_id": workspace_id,
            "image_model": image_model,
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


@pytest.mark.parametrize("route", ["whole", "center"])
def test_models_and_repeated_rounds_stay_in_one_design_task(route):
    db = mongomock.MongoClient().db
    db.whole_tasks.insert_many([
        _task("gpt-1", workspace_id="design-a", batch_id="gpt-round-1"),
        _task("seed-1", workspace_id="design-a", batch_id="seed-round-1",
              image_model="seedream-5-pro-image-to-image", status="running"),
        _task("gpt-2", workspace_id="design-a", batch_id="gpt-round-2", status="failed"),
        _task("another-design", workspace_id="design-b", batch_id="separate-round"),
    ])
    def listed():
        req = _list_request(db)
        return whole_routes.list_tasks(req) if route == "whole" else task_center_routes.list_task_center(req, kind="generate")

    result = listed()
    assert result["total"] == 2
    group = next(item for item in result["items"] if item["id"] == "design-a")
    assert group["candidate_count"] == 3
    assert (group["active_count"], group["succeeded_count"], group["failed_count"]) == (1, 1, 1)
    assert group["status"] == "active"
    assert set(group["image_models"]) == {"gpt-image-2.5-sunburst", "seedream-5-pro-image-to-image"}
    assert group["representative_task_id"] == "gpt-1"

    whole_routes.retry_task("gpt-2", _retry_request(db), BackgroundTasks())
    db.whole_tasks.insert_one(_task("gpt-3", workspace_id="design-a", batch_id="gpt-round-3"))
    result = listed()
    assert result["total"] == 2
    group = next(item for item in result["items"] if item["id"] == "design-a")
    assert group["candidate_count"] == 4
    assert group["active_count"] == 2

    db.whole_tasks.update_one({"_id": "seed-1"}, {"$set": {"status": "succeeded"}})
    whole_routes.delete_candidate("seed-1", _list_request(db))
    group = next(item for item in listed()["items"] if item["id"] == "design-a")
    assert group["candidate_count"] == 3
    assert group["image_models"] == ["gpt-image-2.5-sunburst"]


def test_workspace_group_pagination_keeps_all_models_together():
    db = mongomock.MongoClient().db
    db.whole_tasks.insert_many([
        _task("gpt", workspace_id="new", batch_id="one", created_at=BASE_TIME + timedelta(minutes=1)),
        _task("seed", workspace_id="new", batch_id="two", image_model="seedream-5.5-image-to-image", created_at=BASE_TIME + timedelta(minutes=2)),
        _task("old", workspace_id="old", batch_id="three", created_at=BASE_TIME),
    ])
    first = task_center_routes.list_task_center(_list_request(db), limit=1)
    assert len(first["items"]) == 1
    assert first["items"][0]["id"] == "new"
    assert first["items"][0]["candidate_count"] == 2
    second = task_center_routes.list_task_center(_list_request(db), limit=1, cursor=first["next_cursor"])
    assert [item["id"] for item in second["items"]] == ["old"]


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



def test_task_center_excludes_archived_workflows():
    db = mongomock.MongoClient().db
    db.whole_tasks.insert_one(_task("wtask_current"))
    # Retired records remain in storage, but must not be exposed or inspected.
    db.poster_edit_tasks.insert_one({"_id": "archived_edit", "created_at": BASE_TIME})
    db.background_replace_tasks.insert_one({"_id": "archived_replacement", "created_at": BASE_TIME})

    for kind in ("all", "generate"):
        result = task_center_routes.list_task_center(_list_request(db), kind=kind)
        assert result["total"] == 1
        assert [item["id"] for item in result["items"]] == ["wtask_current"]
        assert result["items"][0]["kind"] == "generate"
    assert db.poster_edit_tasks.count_documents({}) == 1
    assert db.background_replace_tasks.count_documents({}) == 1


@pytest.mark.parametrize("kind", ["edit", "background_replace"])
def test_task_center_rejects_retired_workflow_filters(kind):
    with pytest.raises(HTTPException) as exc_info:
        task_center_routes.list_task_center(_list_request(mongomock.MongoClient().db), kind=kind)
    assert exc_info.value.status_code == 422


def test_task_center_cursor_pagination_keeps_whole_image_groups():
    db = mongomock.MongoClient().db
    db.whole_tasks.insert_many([
        _task("wtask_old", created_at=BASE_TIME),
        _task("wtask_new", created_at=BASE_TIME + timedelta(minutes=1)),
    ])
    first = task_center_routes.list_task_center(_list_request(db), limit=1)
    assert [item["id"] for item in first["items"]] == ["wtask_new"]
    assert first["next_cursor"]
    second = task_center_routes.list_task_center(_list_request(db), limit=1, cursor=first["next_cursor"])
    assert [item["id"] for item in second["items"]] == ["wtask_old"]
    assert second["next_cursor"] is None
