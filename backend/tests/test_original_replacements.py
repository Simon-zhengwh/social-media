from datetime import timedelta
from types import SimpleNamespace

import mongomock
import pytest
from fastapi import BackgroundTasks, HTTPException
from pydantic import ValidationError

from app import schemas
from app.main import app
from app.routes import task_center, whole_image as routes
from app.services import original_replacement_runner, storage, whole_image
from tests.conftest import png_bytes
from tests.test_original_replacement_runner import FakeAgent
from tests.test_whole_image import FakeCoreAI, _generate, _request


@pytest.fixture
def env(settings, store):
    db = mongomock.MongoClient().db
    task_id, task = _generate(db, settings, store, FakeCoreAI())
    for asset_id in ("beef", "environment"):
        image = storage.prepare_upload(settings, store, png_bytes(), asset_id + ".png")
        db.assets.insert_one({"_id": asset_id, "original_name": asset_id + ".png",
                              "path": image.path, "prepared_path": image.prepared_path,
                              "mime_type": "image/png", "width": 320, "height": 240})
    client = FakeAgent()
    client.data = png_bytes(task["canvas"]["width"], task["canvas"]["height"])
    return SimpleNamespace(db=db, settings=settings, store=store, client=client, task_id=task_id,
                           source_id=task["current_version_id"], output_id=task["current_output_id"],
                           request=_request(db, settings, store, client))


def create(env, items=None, **overrides):
    values = {"source_version_id": env.source_id, "source_output_id": env.output_id,
              "replacements": items if items is not None else [{"target": "右下角牛肉", "asset_id": "beef"}]}
    values.update(overrides)
    return routes.create_original_replacement(env.task_id,
        schemas.WholeImageOriginalReplacementRequest(**values), env.request, BackgroundTasks())


def complete(env, task):
    original_replacement_runner.run_task(env.db, env.settings, env.store, env.client, task["id"])
    return original_replacement_runner.get_task(env.db, task["id"])


def apply(env, task, **overrides):
    current = env.db.whole_tasks.find_one({"_id": env.task_id})
    values = {"expected_current_version_id": current["current_version_id"],
              "expected_current_output_id": current["current_output_id"]}
    values.update(overrides)
    return routes.apply_original_replacement(env.task_id, task["id"],
        schemas.WholeImageOriginalReplacementApplyRequest(**values), env.request)


def test_original_replacement_routes_exist():
    paths = app.openapi()["paths"]
    assert "/api/merchants/{merchant_id}/whole-image/tasks/{task_id}/original-replacements" in paths
    assert "/api/merchants/{merchant_id}/whole-image/tasks/{task_id}/original-replacements/{replacement_id}/apply" in paths
    assert "/api/merchants/{merchant_id}/whole-image/tasks/{task_id}/original-replacements/{replacement_id}/refresh-result" in paths
    assert "/api/merchants/{merchant_id}/whole-image/tasks/{task_id}/original-replacements/{replacement_id}/download" in paths


def test_linked_replacement_refresh_reuses_run_and_scopes_to_parent(env):
    task = create(env)
    env.db.background_replace_tasks.update_one({"_id": task["id"]}, {"$set": {
        "status": "failed", "run_id": "existing-run"}})
    background = BackgroundTasks()
    refreshed = routes.refresh_original_replacement(env.task_id, task["id"], env.request, background)
    assert refreshed["status"] == "queued"
    assert refreshed["run_id"] == "existing-run"
    assert len(background.tasks) == 1
    assert env.client.triggers == []
    env.db.whole_tasks.insert_one({"_id": "another-poster"})
    with pytest.raises(HTTPException) as exc:
        routes.refresh_original_replacement("another-poster", task["id"], env.request, BackgroundTasks())
    assert exc.value.status_code == 404


def test_linked_replacement_download_remains_available(env):
    from fastapi.responses import FileResponse

    task = create(env)
    with pytest.raises(HTTPException) as exc:
        routes.download_original_replacement(env.task_id, task["id"], env.request)
    assert exc.value.status_code == 404
    complete(env, task)
    restored = routes.list_original_replacements(env.task_id, env.request)[0]
    assert restored["output"]["download_url"] == (
        f"/api/whole-image/tasks/{env.task_id}/original-replacements/{task['id']}/download")
    response = routes.download_original_replacement(env.task_id, task["id"], env.request)
    assert isinstance(response, FileResponse)
    assert response.media_type == "image/png"
    env.db.whole_tasks.insert_one({"_id": "another-poster"})
    with pytest.raises(HTTPException) as exc:
        routes.download_original_replacement("another-poster", task["id"], env.request)
    assert exc.value.status_code == 404


def test_replacement_uses_selected_output_and_only_selected_material(env):
    task = create(env)
    assert task["whole_task_id"] == env.task_id
    assert task["source_version_id"] == env.source_id
    assert task["source_output_id"] == env.output_id
    assert task["source_version"] == 1
    source = env.db.whole_versions.find_one({"_id": env.source_id})
    assert task["source"]["asset_id"] == source["output_asset_id"]
    complete(env, task)
    assert "右下角牛肉" in env.client.triggers[0][1]
    assert "environment" not in str(env.db.background_replace_tasks.find_one({"_id": task["id"]})["input"])
    assert len(env.client.uploads) == 2
    assert env.db.whole_tasks.find_one({"_id": env.task_id})["current_version_id"] == env.source_id
    assert env.db.whole_versions.count_documents({"task_id": env.task_id}) == 1
    restored = routes.list_original_replacements(env.task_id, env.request)
    assert [item["id"] for item in restored] == [task["id"]]
    assert restored[0]["status"] == "succeeded"


@pytest.mark.parametrize("items", [[], [{"target": " ", "asset_id": "beef"}],
    [{"target": "牛肉", "asset_id": "beef"}] * 6])
def test_invalid_replacement_selection_is_rejected(env, items):
    with pytest.raises(ValidationError):
        create(env, items)
    assert env.db.background_replace_tasks.count_documents({}) == 0


def test_same_original_can_replace_multiple_targets(env):
    task = create(env, [{"target": "左边牛肉", "asset_id": "beef"},
                        {"target": "右边牛肉", "asset_id": "beef"}])
    complete(env, task)
    assert "左边牛肉替换成图2，并且把图1的右边牛肉替换成图2" in env.client.triggers[0][1]


@pytest.mark.parametrize("overrides", [{"source_version_id": "missing"}, {"source_output_id": "missing"}])
def test_invalid_source_does_not_create_a_remote_task(env, overrides):
    with pytest.raises(HTTPException) as exc:
        create(env, **overrides)
    assert exc.value.status_code == 422
    assert env.db.background_replace_tasks.count_documents({}) == 0


def test_transparent_source_and_missing_material_are_rejected(env):
    env.db.whole_versions.update_one({"_id": env.source_id}, {"$set": {"outputs.0.selectable": False}})
    with pytest.raises(HTTPException) as exc:
        create(env)
    assert exc.value.status_code == 422
    env.db.whole_versions.update_one({"_id": env.source_id}, {"$set": {"outputs.0.selectable": True}})
    with pytest.raises(HTTPException):
        create(env, [{"target": "牛肉", "asset_id": "missing"}])
    assert env.db.background_replace_tasks.count_documents({}) == 0


def test_apply_adds_one_version_and_reuses_the_saved_asset(env):
    task = create(env)
    finished = complete(env, task)
    output_id = finished["output"]["asset_id"]
    image = env.db.assets.find_one({"_id": output_id})
    before = env.store.get_bytes(image["path"])
    result = apply(env, task)
    assert result["current_version"] == 2
    version = env.db.whole_versions.find_one({"_id": result["current_version_id"]})
    assert version["operation"] == "original_replace"
    assert version["parent_version_id"] == env.source_id
    assert version["source_output_id"] == env.output_id
    assert version["replacement_task_id"] == task["id"]
    assert version["replacements"] == [{"target": "右下角牛肉", "asset_id": "beef"}]
    assert version["output_asset_id"] == output_id
    assert result["outputs"][0]["role"] == "original_replace_result"
    assert result["edit"]["branches"] == {}
    assert env.store.get_bytes(image["path"]) == before
    assert len(env.client.triggers) == len(env.client.downloads) == 1
    versions = routes.list_versions(env.task_id, env.request)
    assert versions[-1]["replacement_task_id"] == task["id"]
    restored = routes.list_original_replacements(env.task_id, env.request)[0]
    assert restored["applied_version_id"] == version["_id"]
    whole_image.select_version(env.db, env.task_id, env.source_id)
    again = apply(env, task)
    assert again["current_version_id"] == env.source_id
    assert env.db.whole_versions.count_documents({"task_id": env.task_id}) == 2


def test_apply_preserves_old_source_after_newer_edits(env):
    task = create(env)
    complete(env, task)
    newer = env.db.whole_versions.find_one({"_id": env.source_id})
    newer.update(_id="newer", version=2, operation="edit")
    env.db.whole_versions.insert_one(newer)
    whole_image.select_version(env.db, env.task_id, "newer")
    with pytest.raises(HTTPException) as exc:
        apply(env, task, expected_current_version_id=env.source_id)
    assert exc.value.status_code == 409
    result = apply(env, task)
    assert result["current_version"] == 3
    version = env.db.whole_versions.find_one({"_id": result["current_version_id"]})
    assert version["parent_version_id"] == env.source_id
    assert env.db.whole_versions.find_one({"_id": "newer"})


@pytest.mark.parametrize("state", ["queued", "failed", "partial"])
def test_unfinished_or_ambiguous_result_cannot_be_applied(env, state):
    task = create(env)
    env.db.background_replace_tasks.update_one({"_id": task["id"]}, {"$set": {"status": state}})
    with pytest.raises(HTTPException) as exc:
        apply(env, task)
    assert exc.value.status_code == 409
    assert env.db.whole_versions.count_documents({"task_id": env.task_id}) == 1


def test_apply_during_edit_does_not_overwrite_it(env):
    task = create(env)
    complete(env, task)
    env.db.whole_tasks.update_one({"_id": env.task_id}, {"$set": {"edit.status": "running"}})
    with pytest.raises(HTTPException) as exc:
        apply(env, task)
    assert exc.value.status_code == 409
    assert env.db.whole_versions.count_documents({"task_id": env.task_id}) == 1


def test_other_tasks_cannot_apply_the_result(env):
    task = create(env)
    complete(env, task)
    env.db.background_replace_tasks.update_one({"_id": task["id"]}, {"$set": {"whole_task_id": "other"}})
    with pytest.raises(HTTPException) as exc:
        apply(env, task)
    assert exc.value.status_code == 404


def test_apply_failure_does_not_delete_shared_image(env, monkeypatch):
    task = create(env)
    finished = complete(env, task)
    original_update = env.db.whole_tasks.update_one

    def fail_commit(query, update, *args, **kwargs):
        if update.get("$set", {}).get("current_version_id"):
            raise RuntimeError("database unavailable")
        return original_update(query, update, *args, **kwargs)

    monkeypatch.setattr(env.db.whole_tasks, "update_one", fail_commit)
    with pytest.raises(RuntimeError, match="database unavailable"):
        apply(env, task)
    image = env.db.assets.find_one({"_id": finished["output"]["asset_id"]})
    assert image and env.store.get_bytes(image["path"])
    assert env.db.whole_versions.count_documents({"task_id": env.task_id}) == 1
    assert env.db.whole_tasks.find_one({"_id": env.task_id})["edit"]["status"] == "idle"
    monkeypatch.setattr(env.db.whole_tasks, "update_one", original_update)
    assert apply(env, task)["current_version"] == 2


def test_task_center_links_associated_replacements_to_the_whole_task(env):
    task = create(env)
    groups = task_center.list_task_center(env.request, kind="original_replace")["items"]
    group = next(item for item in groups if item["id"] == task["id"])
    assert group["whole_task_id"] == env.task_id
    assert group["kind"] == "original_replace"


def test_restart_does_not_resume_retired_standalone_tasks(env):
    env.db.background_replace_tasks.insert_one({
        "_id": "retired-task", "status": "running", "run_id": "retired-run"})
    assert original_replacement_runner.mark_interrupted(env.db) == 0
    original_replacement_runner.resume_tasks(env.db, env.settings, env.store, env.client)
    assert env.client.triggers == []
    assert env.client.polls == []


def test_only_linked_original_replacements_protect_materials():
    from app.services.asset_records import asset_referenced

    db = mongomock.MongoClient().db
    db.background_replace_tasks.insert_one({"_id": "archived", "input": {
        "source_asset_id": "archived-source", "replacements": [{"asset_id": "shared-material"}]}})
    assert not asset_referenced(db, "archived-source")
    assert not asset_referenced(db, "shared-material")
    db.background_replace_tasks.insert_one({"_id": "linked", "whole_task_id": "poster", "input": {
        "source_asset_id": "linked-source", "replacements": [{"asset_id": "shared-material"}]}})
    assert asset_referenced(db, "linked-source")
    assert asset_referenced(db, "shared-material")


def test_version_preparation_failure_releases_the_apply_lock(env, monkeypatch):
    task = create(env)
    complete(env, task)
    with monkeypatch.context() as patch:
        patch.setattr(whole_image, "_next_version", lambda *_: (_ for _ in ()).throw(RuntimeError("read unavailable")))
        with pytest.raises(RuntimeError, match="read unavailable"):
            apply(env, task)
    assert env.db.whole_tasks.find_one({"_id": env.task_id})["edit"]["status"] == "idle"
    assert apply(env, task)["current_version"] == 2


def test_concurrent_retry_cannot_mark_an_uncommitted_version_as_applied(env, monkeypatch):
    task = create(env)
    complete(env, task)
    original_insert = env.db.whole_versions.insert_one
    retried = []

    def insert_then_retry(version, *args, **kwargs):
        result = original_insert(version, *args, **kwargs)
        try:
            apply(env, task)
            retried.append("returned")
        except HTTPException as exc:
            retried.append(exc.status_code)
        return result

    original_update = env.db.whole_tasks.update_one

    def fail_commit(query, update, *args, **kwargs):
        if update.get("$set", {}).get("current_version_id"):
            raise RuntimeError("commit unavailable")
        return original_update(query, update, *args, **kwargs)

    monkeypatch.setattr(env.db.whole_versions, "insert_one", insert_then_retry)
    monkeypatch.setattr(env.db.whole_tasks, "update_one", fail_commit)
    with pytest.raises(RuntimeError, match="commit unavailable"):
        apply(env, task)
    assert retried == [409]
    assert not original_replacement_runner.get_task(env.db, task["id"]).get("applied_version_id")
    assert env.db.whole_versions.count_documents({"task_id": env.task_id}) == 1


def test_linked_replacement_resumes_existing_run_after_restart(env):
    task = create(env)
    now = whole_image.utcnow()
    env.db.background_replace_tasks.update_one({"_id": task["id"]}, {"$set": {
        "status": "running", "run_id": "saved-run", "started_at": now, "deadline_at": now + timedelta(minutes=45)}})
    whole_image.mark_interrupted(env.db)
    assert original_replacement_runner.mark_interrupted(env.db) == 1
    original_replacement_runner.resume_tasks(env.db, env.settings, env.store, env.client)
    restored = routes.list_original_replacements(env.task_id, env.request)[0]
    assert restored["status"] == "succeeded"
    assert restored["source_version_id"] == env.source_id
    assert env.client.triggers == []
    assert env.client.polls == ["saved-run"]
    assert env.db.whole_tasks.find_one({"_id": env.task_id})["current_version_id"] == env.source_id


def test_normal_edit_of_applied_result_does_not_inherit_replacement_metadata(env):
    task = create(env)
    complete(env, task)
    applied = apply(env, task)
    client = FakeCoreAI()
    request = _request(env.db, env.settings, env.store, client)
    payload = schemas.WholeImageEditRequest(instruction="修改标题", source_version_id=applied["current_version_id"],
                                           source_output_id=applied["current_output_id"])
    routes.create_edit(env.task_id, payload, request, BackgroundTasks())
    whole_image.run_edit(env.db, env.settings, env.store, client, env.task_id,
                         payload.instruction, payload.source_version_id, payload.source_output_id)
    latest = routes.list_versions(env.task_id, request)[-1]
    assert latest["operation"] == "edit"
    assert latest["replacement_task_id"] is None
    assert latest["replacements"] == []
    assert latest["parent_version_id"] == applied["current_version_id"]
    assert all(item["role"] != "original_replace_result" for item in latest["outputs"])


def test_metadata_failure_after_commit_can_be_retried_without_reverting_selection(env, monkeypatch):
    task = create(env)
    complete(env, task)
    original_update = env.db.background_replace_tasks.update_one

    def fail_metadata(query, update, *args, **kwargs):
        if "applied_version_id" in update.get("$set", {}):
            raise RuntimeError("metadata unavailable")
        return original_update(query, update, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(env.db.background_replace_tasks, "update_one", fail_metadata)
        with pytest.raises(RuntimeError, match="metadata unavailable"):
            apply(env, task)
    whole_image.select_version(env.db, env.task_id, env.source_id)
    assert apply(env, task)["current_version_id"] == env.source_id
    assert env.db.whole_versions.count_documents({"task_id": env.task_id}) == 2
    assert original_replacement_runner.get_task(env.db, task["id"])["applied_version_id"]
