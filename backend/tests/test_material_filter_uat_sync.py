from __future__ import annotations

import hashlib
import io
import time

import httpx
import mongomock
import pytest
from PIL import Image

from app.config import Settings
from app.services import material_filter_uat_sync as sync_service
from app.services import material_filters as store
from app.services.material_filter_files import SourceEntry, SourceSnapshot
from app.services.scoped_db import ScopedDatabase


def _jpeg(size=(320, 240), color=(180, 90, 50), quality=90) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def _noise_jpeg(size=(1600, 1200), quality=95) -> bytes:
    import os

    image = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


@pytest.fixture
def db() -> ScopedDatabase:
    return ScopedDatabase(mongomock.MongoClient().db, "merchant-a")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        storage_dir=str(tmp_path / "storage"),
        am_bypass_local=True,
        social_media_origin="http://127.0.0.1:5173",
        core_ai_api_key="test-key",
        material_filter_uat_base_url="https://uat.test",
        material_filter_uat_max_body_mb=9.0,
    )


def _scheme(db, settings) -> dict:
    folder, _ = store.add_folder(db, "\\\\nas\\share\\照片", readable_check=lambda _: None)
    return store.create_scheme(db, settings, folder, name="方案")


def _picked(db, settings, scheme, *, data: bytes, name: str = "菜品.jpg") -> dict:
    directory = store.scheme_directory(settings, scheme)
    (directory / "passed").mkdir(parents=True, exist_ok=True)
    (directory / "passed" / name).write_bytes(data)
    entry = SourceEntry(
        relative_path=name, name=name, size=len(data), mtime_ns=1, absolute_path=str(name)
    )
    snapshot = SourceSnapshot(
        relative_path=name,
        size=len(data),
        mtime_ns=1,
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )
    file_doc = store.upsert_file(
        db,
        scheme,
        entry,
        size=snapshot.size,
        mtime_ns=snapshot.mtime_ns,
        sha256=snapshot.sha256,
        latest_evaluation_id=None,
    )
    evaluation = store.save_evaluation(
        db,
        scheme,
        file_doc,
        snapshot,
        result=None,
        decision="pass",
        error=None,
        preview_path=None,
        output_path=f"passed/{name}",
        output_name=name,
        copy_status="copied",
        copy_error=None,
        short_id="abcd1234",
    )
    store.update_metadata(db, evaluation, display_name="红烧肉特写")
    return store.set_manual_pick(db, evaluation, picked=True)


def _transport(uploads: list[dict], *, tag_status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/assets"):
            return httpx.Response(200, json={"items": [], "total": 0})
        if request.method == "GET" and path.endswith("/am-assets"):
            return httpx.Response(200, json={"items": [], "total": 0})
        if request.method == "POST" and path.endswith("/assets"):
            body = request.read()
            uploads.append({"path": path, "body": body})
            return httpx.Response(
                201,
                json={
                    "id": "asset_1",
                    "original_name": "red-braised-pork.jpg",
                    "url": "https://cdn.test/a.jpg",
                    "tags": [],
                },
            )
        if request.method == "PUT" and path.endswith("/tags"):
            uploads.append({"path": path, "body": request.read()})
            return httpx.Response(tag_status, json={"id": "asset_1", "tags": []})
        return httpx.Response(404, json={"detail": "not found"})

    return httpx.MockTransport(handler)


def _wait(db, sync_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        document = sync_service.get_sync(db, sync_id)
        if document["status"] not in {"queued", "running"}:
            return document
        time.sleep(0.02)
    raise AssertionError("sync did not finish")


def test_sync_uploads_picked_photo_with_clean_name_and_capped_tags(db, settings):
    scheme = _scheme(db, settings)
    evaluation = _picked(db, settings, scheme, data=_jpeg())
    uploads: list[dict] = []
    document = sync_service.start_sync(
        db, settings, scheme, cookie="AMSessionId=ok", transport=_transport(uploads)
    )
    finished = _wait(db, document["_id"])

    assert finished["status"] == "succeeded"
    assert finished["counters"]["uploaded"] == 1
    stored = store.get_evaluation(db, evaluation["_id"])
    assert stored["uat_asset_id"] == "asset_1"
    assert stored["uat_asset_name"] == "red-braised-pork.jpg"

    upload = next(item for item in uploads if item["path"].endswith("/assets"))
    assert "红烧肉特写".encode() in upload["body"]

    tags_request = next(item for item in uploads if item["path"].endswith("/tags"))
    assert "素材筛选" in tags_request["body"].decode()


def test_sync_recompresses_when_the_upload_body_is_too_large(db, settings):
    settings.material_filter_uat_max_body_mb = 0.05
    scheme = _scheme(db, settings)
    _picked(db, settings, scheme, data=_noise_jpeg())
    uploads: list[dict] = []
    document = sync_service.start_sync(
        db, settings, scheme, cookie="AMSessionId=ok", transport=_transport(uploads)
    )
    finished = _wait(db, document["_id"])

    assert finished["counters"]["compressed"] == 1
    upload = next(item for item in uploads if item["path"].endswith("/assets"))
    assert len(upload["body"]) < 120_000
    stored = db.material_filter_evaluations.find_one({"uat_asset_id": "asset_1"})
    assert stored["uat_recompressed"] is True


def test_dry_run_lists_names_without_uploading(db, settings):
    scheme = _scheme(db, settings)
    evaluation = _picked(db, settings, scheme, data=_jpeg())
    uploads: list[dict] = []
    document = sync_service.start_sync(
        db,
        settings,
        scheme,
        cookie="AMSessionId=ok",
        dry_run=True,
        transport=_transport(uploads),
    )
    finished = _wait(db, document["_id"])

    assert finished["status"] == "succeeded"
    assert not uploads
    assert finished["items"][0]["asset_name"].endswith(".jpg")
    assert store.get_evaluation(db, evaluation["_id"]).get("uat_asset_id") is None


def test_sync_requires_a_cookie(db, settings):
    scheme = _scheme(db, settings)
    with pytest.raises(sync_service.SyncError) as excinfo:
        sync_service.start_sync(db, settings, scheme, cookie="")
    assert excinfo.value.code == "uat_cookie_required"


def test_failed_upload_is_recorded_per_item(db, settings):
    scheme = _scheme(db, settings)
    evaluation = _picked(db, settings, scheme, data=_jpeg())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"items": [], "total": 0})
        return httpx.Response(401, json={"detail": "unauthorized"})

    document = sync_service.start_sync(
        db, settings, scheme, cookie="AMSessionId=expired", transport=httpx.MockTransport(handler)
    )
    finished = _wait(db, document["_id"])
    assert finished["status"] == "failed"
    assert finished["counters"]["failed"] == 1
    stored = store.get_evaluation(db, evaluation["_id"])
    assert stored.get("uat_asset_id") is None
    assert "401" in stored["uat_sync_error"]
