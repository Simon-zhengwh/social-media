from types import SimpleNamespace

import httpx
import mongomock
import pytest
from fastapi import BackgroundTasks, HTTPException

from app.services import maintenance
from app.services.media_object_store import MediaObjectStore
from app.services.object_store import ObjectStoreError
from app.services.scoped_db import ScopedDatabase


def test_am_media_accepts_artifacts_without_changing_logical_keys():
    assert MediaObjectStore._kind("artifacts/task/editor.html") == "OUTPUTS"
    assert MediaObjectStore._kind("artifacts/task/result.zip") == "OUTPUTS"
    assert MediaObjectStore._kind("outputs/poster.png") == "OUTPUTS"


def test_artifact_download_has_its_own_limit(settings):
    settings.max_download_bytes = 3
    settings.max_agent_artifact_bytes = 8
    db = mongomock.MongoClient().db
    for key in ("artifacts/editor.html", "outputs/poster.png"):
        db.media_objects.insert_one({"_id": key, "status": "ready", "url": "https://media.example/file"})
    http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"12345")))
    store = MediaObjectStore(settings, db, SimpleNamespace(http=http), "m1")
    try:
        assert store.get_bytes("artifacts/editor.html") == b"12345"
        with pytest.raises(ObjectStoreError):
            store.get_bytes("outputs/poster.png")
        settings.max_agent_artifact_bytes = 4
        with pytest.raises(ObjectStoreError):
            store.get_bytes("artifacts/editor.html")
    finally:
        store.close()
        http.close()


def test_maintenance_waits_for_agent_tests():
    db = mongomock.MongoClient().db
    maintenance.initialize(db)
    db.agent_test_runs.insert_one({"_id": "paid-run", "status": "triggering"})
    assert maintenance.status(db)["drained"] is False
    db.agent_test_runs.update_one({"_id": "paid-run"}, {"$set": {"status": "completed"}})
    assert maintenance.status(db)["drained"] is True


def scoped_request(raw, store, settings, merchant="m1"):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db=raw, store=store, settings=settings)),
        state=SimpleNamespace(db=ScopedDatabase(raw, merchant), store=store, merchant_id=merchant),
    )


def test_artifact_preview_cannot_read_another_merchant(settings, store):
    from app.routes import whole_image

    raw = mongomock.MongoClient().db
    store.put_bytes("artifacts/editor.html", b"<p>private</p>", "text/html")
    raw.whole_artifacts.insert_one({"_id": "private", "merchant_id": "m2", "kind": "html", "path": "artifacts/editor.html"})
    req = scoped_request(raw, store, settings)
    for handler in (whole_image.preview_artifact, whole_image.download_artifact):
        with pytest.raises(HTTPException) as exc:
            handler("private", req)
        assert exc.value.status_code == 404


def test_replacement_list_cannot_read_another_merchant(settings, store):
    from app.routes import whole_image

    raw = mongomock.MongoClient().db
    raw.whole_tasks.insert_one({"_id": "foreign", "merchant_id": "m2"})
    with pytest.raises(HTTPException) as exc:
        whole_image.list_original_replacements("foreign", scoped_request(raw, store, settings))
    assert exc.value.status_code == 404


def test_artifact_output_urls_keep_merchant_scope():
    from app.routes import whole_image

    raw = mongomock.MongoClient().db
    db = ScopedDatabase(raw, "m1")
    db.assets.insert_one({"_id": "poster", "width": 10, "height": 10})
    output = whole_image._output_out(db, task_id="t1", version_id="v1", version_number=1,
        output={"id": "out", "asset_id": "poster", "artifacts": [{"id": "html1", "kind": "html"}]})
    assert output["artifacts"][0]["preview_url"] == "/api/merchants/m1/whole-image/artifacts/html1/preview"
    assert output["artifacts"][0]["download_url"] == "/api/merchants/m1/whole-image/artifacts/html1/download"


def test_replacement_imports_selected_am_material_in_request_scope(settings, store, monkeypatch):
    from app.routes import whole_image
    from app.schemas import WholeImageOriginalReplacementRequest

    raw = mongomock.MongoClient().db
    req = scoped_request(raw, store, settings)
    req.app.state.core_ai = object()
    req.state.db.whole_tasks.insert_one({"_id": "task1"})
    resolved = []
    monkeypatch.setattr(whole_image.request_assets, "resolve_asset_id",
        lambda request, asset_id: resolved.append((request.state.merchant_id, asset_id)) or "imported")
    captured = {}
    def create(db, task_id, **values):
        captured.update(merchant=db.merchant_id, task=task_id, **values)
        return {"_id": "replacement1", "merchant_id": db.merchant_id}
    monkeypatch.setattr(whole_image.original_replacement, "create", create)
    background = BackgroundTasks()
    whole_image.create_original_replacement("task1", WholeImageOriginalReplacementRequest(
        source_version_id="v1", source_output_id="out1",
        replacements=[{"target": "background", "asset_id": "am:selected"}]), req, background)
    assert resolved == [("m1", "am:selected")]
    assert captured["merchant"] == "m1"
    assert captured["replacements"][0]["asset_id"] == "imported"
    assert background.tasks[0].args[0].merchant_id == "m1"
    assert background.tasks[0].args[2] is store


def test_replacement_creation_and_result_urls_keep_scope():
    from app.services import original_replacement_runner as runner

    db = ScopedDatabase(mongomock.MongoClient().db, "m1")
    for asset_id in ("source", "material"):
        db.assets.insert_one({"_id": asset_id, "mime_type": "image/png", "width": 10, "height": 10})
    doc = runner.create_task(db, source_asset_id="source", whole_task_id="task1",
                             replacements=[{"target": "background", "asset_id": "material"}])
    assert doc["merchant_id"] == "m1"
    doc["output"] = {"asset_id": "result", "width": 10, "height": 10}
    output = runner.task_out(doc)["output"]
    assert output["preview_url"] == "/api/merchants/m1/assets/result/file"
    assert output["download_url"] == f"/api/merchants/m1/whole-image/tasks/task1/original-replacements/{doc['_id']}/download"


def test_startup_recovers_work_using_each_merchants_database(settings, store, monkeypatch):
    from app import main

    raw = mongomock.MongoClient().db
    for merchant_id in ("m1", "m2"):
        raw.background_replace_tasks.insert_one({"merchant_id": merchant_id, "whole_task_id": "task", "status": "running"})
        raw.agent_test_runs.insert_one({"merchant_id": merchant_id, "status": "running"})
    raw.background_replace_tasks.insert_one({"merchant_id": "finished", "whole_task_id": "old", "status": "succeeded"})
    raw.agent_test_runs.insert_one({"merchant_id": "finished", "status": "completed"})
    app = SimpleNamespace(state=SimpleNamespace(raw_db=raw, settings=settings, local_store=store, core_ai=object()))
    workers = []
    monkeypatch.setattr(main.threading, "Thread", lambda **values: SimpleNamespace(start=lambda: workers.append(values)))
    monkeypatch.setattr(main.original_replacement_runner, "mark_interrupted", lambda db: ["replacement"])
    monkeypatch.setattr(main.agent_test_service, "recover", lambda db: ["batch"])
    main._resume_original_replacements(app)
    main._resume_agent_tests(app)
    assert len(workers) == 4
    assert sorted(worker["args"][0].merchant_id for worker in workers) == ["m1", "m1", "m2", "m2"]


def test_agent_status_index_supports_global_maintenance_without_table_scans():
    from app import main

    raw = mongomock.MongoClient().db
    main._initialize_indexes(raw)
    indexes = [list(index["key"].keys()) for index in raw.agent_test_runs.list_indexes()]
    assert any(keys[:1] == ["status"] for keys in indexes)


def test_editor_endpoints_are_only_mounted_under_merchant_authentication():
    from app.main import app

    paths = set(app.openapi()["paths"])
    assert "/api/merchants/{merchant_id}/whole-image/artifacts/{artifact_id}/preview" in paths
    assert "/api/merchants/{merchant_id}/agent-tests/batches" in paths
    assert not any(path.startswith(("/api/whole-image", "/api/agent-tests")) for path in paths)
