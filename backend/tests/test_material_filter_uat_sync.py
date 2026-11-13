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


def test_dry_run_rejects_an_expired_cookie_instead_of_pretending_success(db, settings):
    scheme = _scheme(db, settings)
    _picked(db, settings, scheme, data=_jpeg())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "am_http_401"}})

    with pytest.raises(sync_service.SyncError) as excinfo:
        sync_service.start_sync(
            db,
            settings,
            scheme,
            cookie="AMSessionId=expired",
            dry_run=True,
            transport=httpx.MockTransport(handler),
        )
    assert excinfo.value.code == sync_service.COOKIE_INVALID_CODE
    assert "Cookie" in excinfo.value.message

    failed = db[sync_service.SYNC_COLLECTION].find_one({"scheme_id": scheme["_id"]})
    assert failed["status"] == "failed", "预览失败必须解除排队锁，允许更新 Cookie 后重试"
    assert failed["finished_at"] is not None
    assert "Cookie" in failed["error"]

    retried = sync_service.start_sync(
        db, settings, scheme, cookie="AMSessionId=renewed", dry_run=True,
        transport=_transport([]),
    )
    assert retried["_id"] != failed["_id"]
    assert retried["status"] == "succeeded"
    assert retried["counters"]["total"] == 1


def test_dry_run_records_a_filename_error_instead_of_leaving_a_queued_job(db, settings):
    scheme = _scheme(db, settings)
    evaluation = _picked(db, settings, scheme, data=_jpeg())
    db[store.EVALUATION_COLLECTION].update_one(
        {"_id": evaluation["_id"]},
        {"$set": {"naming_mode": "menu_match", "display_name": ""}},
    )

    with pytest.raises(sync_service.SyncError) as excinfo:
        sync_service.start_sync(
            db, settings, scheme, cookie="AMSessionId=ok", dry_run=True,
            transport=_transport([]),
        )

    assert excinfo.value.code == "invalid_menu_filename"
    failed = db[sync_service.SYNC_COLLECTION].find_one({"scheme_id": scheme["_id"]})
    assert failed["status"] == "failed"
    assert failed["finished_at"] is not None
    assert "菜单双语名称" in failed["error"]


def test_preflight_stops_the_run_when_the_cookie_is_expired(db, settings):
    scheme = _scheme(db, settings)
    _picked(db, settings, scheme, data=_jpeg())
    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(401, json={"error": {"code": "am_http_401"}})
        posts.append(request.url.path)
        return httpx.Response(201, json={"id": "asset_1"})

    document = sync_service.start_sync(
        db, settings, scheme, cookie="AMSessionId=expired", transport=httpx.MockTransport(handler)
    )
    finished = _wait(db, document["_id"])

    assert finished["status"] == "failed"
    assert not posts
    assert "Cookie" in finished["error"]


def test_preflight_reports_a_rejected_origin_separately_from_an_expired_cookie(db, settings):
    scheme = _scheme(db, settings)
    _picked(db, settings, scheme, data=_jpeg())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"items": [], "total": 0})
        return httpx.Response(
            403, json={"error": {"code": "csrf_origin_rejected", "message": "写请求来源不受信任"}}
        )

    document = sync_service.start_sync(
        db, settings, scheme, cookie="AMSessionId=ok", transport=httpx.MockTransport(handler)
    )
    finished = _wait(db, document["_id"])

    assert finished["status"] == "failed"
    assert "SOCIAL_MEDIA_ORIGIN" in finished["error"]
    assert "写请求来源不受信任" in finished["error"]


def _reset_error() -> httpx.ReadError:
    return httpx.ReadError("[Errno 10054] 远程主机强迫关闭了一个现有的连接。")


def test_reset_during_upload_surfaces_the_hidden_401(db, settings):
    scheme = _scheme(db, settings)
    _picked(db, settings, scheme, data=_jpeg(), name="a.jpg")
    _picked(db, settings, scheme, data=_jpeg(color=(10, 20, 30)), name="b.jpg")
    session = {"expired": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            # 会话在上传过程中过期：预检通过，UAT 在校验阶段直接关连接。
            if session["expired"]:
                return httpx.Response(401, json={"error": {"code": "am_http_401"}})
            return httpx.Response(200, json={"items": [], "total": 0})
        session["expired"] = True
        raise _reset_error()

    document = sync_service.start_sync(
        db, settings, scheme, cookie="AMSessionId=expired", transport=httpx.MockTransport(handler)
    )
    finished = _wait(db, document["_id"])

    assert finished["status"] == "failed"
    assert finished["counters"]["failed"] == 1
    assert finished["counters"]["done"] == 1, "失效会话不再继续上传剩下的照片"
    assert "Cookie" in finished["error"]
    assert "10054" not in finished["items"][0]["message"], "真实的 401 不再被网络错误掩盖"


def test_reset_with_a_live_session_is_retried_once(db, settings):
    scheme = _scheme(db, settings)
    evaluation = _picked(db, settings, scheme, data=_jpeg())
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            return httpx.Response(200, json={"items": [], "total": 0})
        if request.method == "POST" and path.endswith("/assets"):
            attempts.append(path)
            if len(attempts) == 1:
                raise _reset_error()
            return httpx.Response(
                201, json={"id": "asset_1", "original_name": "red-braised-pork.jpg", "url": "u"}
            )
        return httpx.Response(200, json={"id": "asset_1", "tags": []})

    document = sync_service.start_sync(
        db, settings, scheme, cookie="AMSessionId=ok", transport=httpx.MockTransport(handler)
    )
    finished = _wait(db, document["_id"])

    assert finished["status"] == "succeeded"
    assert finished["counters"]["uploaded"] == 1
    assert len(attempts) == 2
    assert store.get_evaluation(db, evaluation["_id"])["uat_asset_id"] == "asset_1"


def test_heic_copies_are_uploaded_as_jpeg(db, settings):
    scheme = _scheme(db, settings)
    # 本地副本保留 .heic；AM 存原文件不转码，浏览器无法渲染 HEIC，所以上传前要转 JPEG。
    _picked(db, settings, scheme, data=_jpeg(), name="IMG_0001.HEIC")
    uploads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            return httpx.Response(200, json={"items": [], "total": 0})
        if request.method == "POST" and path.endswith("/assets"):
            uploads.append({"body": request.read(), "content_type": request.headers.get("content-type", "")})
            return httpx.Response(201, json={"id": "asset_1", "original_name": "红烧肉特写.jpg"})
        return httpx.Response(200, json={"id": "asset_1", "tags": []})

    document = sync_service.start_sync(
        db, settings, scheme, cookie="AMSessionId=ok", transport=httpx.MockTransport(handler)
    )
    finished = _wait(db, document["_id"])

    assert finished["status"] == "succeeded"
    assert finished["counters"]["compressed"] == 1
    assert uploads[0]["content_type"].startswith("multipart/form-data")
    assert b'filename="' in uploads[0]["body"]
    assert ".jpg" in uploads[0]["body"].split(b'filename="')[1].split(b'"')[0].decode()
    assert ".heic" not in uploads[0]["body"].decode("utf-8", "ignore")


def test_dry_run_plans_a_jpeg_name_for_heic_copies(db, settings):
    scheme = _scheme(db, settings)
    _picked(db, settings, scheme, data=_jpeg(), name="IMG_0002.HEIC")
    document = sync_service.start_sync(
        db, settings, scheme, cookie="AMSessionId=ok", dry_run=True, transport=_transport([])
    )
    finished = _wait(db, document["_id"])

    assert finished["status"] == "succeeded"
    assert finished["items"][0]["asset_name"].endswith(".jpg")


def test_bare_session_id_is_wrapped_into_a_cookie_header(db, settings):
    scheme = _scheme(db, settings)
    _picked(db, settings, scheme, data=_jpeg())
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie", ""))
        if request.method == "GET":
            return httpx.Response(200, json={"items": [], "total": 0})
        return httpx.Response(201, json={"id": "asset_1", "original_name": "red-braised-pork.jpg"})

    document = sync_service.start_sync(
        db, settings, scheme, cookie="20f2f627-c5a7-40d3-8bcf-227bb5ad27f3",
        transport=httpx.MockTransport(handler),
    )
    finished = _wait(db, document["_id"])

    assert finished["status"] == "succeeded"
    assert seen and all(item == "AMSessionId=20f2f627-c5a7-40d3-8bcf-227bb5ad27f3" for item in seen)


def test_collect_names_stops_when_the_deployed_list_repeats_a_page():
    # 部署版 /assets 忽略 skip 且不返回 total；同名分页不能变成死循环。
    calls: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get("skip"))
        return httpx.Response(
            200, json=[{"id": f"asset_{index}", "original_name": f"图{index}.jpg"} for index in range(120)]
        )

    with httpx.Client(base_url="https://uat.test", transport=httpx.MockTransport(handler)) as client:
        names = sync_service.collect_names(client, "merchant-a", "/assets")

    assert len(names) == 120
    assert len(calls) == 2


def test_full_cookie_header_is_kept_verbatim(db, settings):
    assert sync_service.session_cookie(" AMSessionId=abc; other=1 ") == "AMSessionId=abc; other=1"
    assert sync_service.session_cookie("") == ""


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


@pytest.mark.parametrize("status,uploaded,linked,expected", [
    ("queued", 0, 0, "failed"),
    ("running", 89, 0, "partial"),
    ("running", 0, 1, "partial"),
])
def test_restart_releases_sync_without_losing_progress(db, status, uploaded, linked, expected):
    counters = {"total": 212, "done": uploaded + linked, "uploaded": uploaded,
                "compressed": 0, "linked": linked, "failed": 0}
    items = [{"name": "photo.jpg", "status": "uploaded"}] if uploaded else []
    db.material_filter_syncs.insert_one({
        "_id": "stale", "status": status, "counters": counters,
        "items": items, "current": "next.jpg", "finished_at": None,
    })
    db.material_filter_syncs.insert_one({"_id": "finished", "status": "succeeded"})
    db.material_filter_syncs.raw.insert_one({
        "_id": "foreign", "merchant_id": "other", "status": "running",
    })

    assert sync_service.mark_interrupted(db) == 1
    recovered = sync_service.get_sync(db, "stale")
    assert recovered["status"] == expected
    assert recovered["counters"] == counters
    assert recovered["items"] == items
    assert recovered["current"] == ""
    assert recovered["finished_at"] is not None
    assert "重启" in recovered["error"]
    assert sync_service.get_sync(db, "finished")["status"] == "succeeded"
    assert db.material_filter_syncs.raw.find_one({"_id": "foreign"})["status"] == "running"
    assert sync_service.mark_interrupted(db) == 0


def test_restart_retry_uploads_only_remaining_picked_photos(db, settings):
    scheme = _scheme(db, settings)
    uploaded = _picked(db, settings, scheme, data=_jpeg(), name="uploaded.jpg")
    remaining = _picked(db, settings, scheme, data=_jpeg(), name="remaining.jpg")
    db.material_filter_evaluations.update_one(
        {"_id": uploaded["_id"]}, {"$set": {"uat_asset_id": "asset_existing"}},
    )
    db.material_filter_syncs.insert_one({
        "_id": "stale", "scheme_id": scheme["_id"], "status": "running",
        "counters": {"uploaded": 1},
    })
    sync_service.mark_interrupted(db)
    uploads = []
    retried = sync_service.start_sync(
        db, settings, scheme, cookie="AMSessionId=ok", transport=_transport(uploads),
    )
    finished = _wait(db, retried["_id"])

    assert retried["_id"] != "stale"
    assert finished["status"] == "succeeded"
    assert finished["counters"]["total"] == finished["counters"]["uploaded"] == 1
    assert len([item for item in uploads if item["path"].endswith("/assets")]) == 1
    assert store.get_evaluation(db, uploaded["_id"])["uat_asset_id"] == "asset_existing"
    assert store.get_evaluation(db, remaining["_id"])["uat_asset_id"] == "asset_1"
