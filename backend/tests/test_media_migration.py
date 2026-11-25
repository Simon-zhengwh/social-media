from __future__ import annotations

import base64
import hashlib
import io
import json
import time

import httpx
import mongomock
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.routes import media_migration as migration_routes
from app.services import media_migration as service
from app.services.media_migration import MediaMigrationError

COOKIE = "AMSessionId=secret-session-value"
PROD_COOKIE = "prod-session-token"


def _jpeg(size=(320, 240), color=(180, 90, 50)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _webp(size=(160, 120)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (30, 120, 200)).save(buffer, format="WEBP")
    return buffer.getvalue()


def _noisy_jpeg(size=(1200, 900)) -> bytes:
    import os

    buffer = io.BytesIO()
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(
        buffer, format="JPEG", quality=98
    )
    return buffer.getvalue()


def _item(
    source_id: str,
    *,
    name: str | None = None,
    media_format: str = "JPEG",
    tags: list[str] | None = None,
    kind: str = "IMAGE",
    url: str | None = None,
    updated_time: str = "2026-05-01T00:00:00Z",
) -> dict:
    return {
        "id": source_id,
        "name": name or f"{source_id}.jpg",
        "url": url if url is not None else f"https://cdn.test/{source_id}",
        "key": f"library/{source_id}",
        "media_library_type": kind,
        "media_library_format": media_format,
        "tags": list(tags or []),
        "size": "1024",
        "thumbnail_image_url": f"https://cdn.test/thumb/{source_id}",
        "updated_time": updated_time,
    }


class FakeAMConsole:
    """Both AM Consoles plus the CDN behind one MockTransport."""

    def __init__(
        self,
        *,
        uat: list[dict] | None = None,
        prod: list[dict] | None = None,
        files: dict[str, bytes] | None = None,
    ) -> None:
        self.libraries = {"uat.test": list(uat or []), "prod.test": list(prod or [])}
        self.files = dict(files or {})
        self.uploads: list[dict] = []
        self.search_calls: list[dict] = []
        self.download_headers: list[dict] = []
        self.reject: dict[str, int] = {}
        self.upload_status = 201
        self.delay = 0.0
        self.uploaded = 0
        self.default_bytes = _jpeg()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if self.delay:
            time.sleep(self.delay)
        if host == "cdn.test":
            # The CDN must never see the AM session.
            self.download_headers.append(
                {"cookie": request.headers.get("cookie"), "path": path}
            )
            return httpx.Response(200, content=self.files.get(path, self.default_bytes))
        if host in self.reject:
            return httpx.Response(
                self.reject[host], json={"error": {"code": "auth", "message": "denied"}}
            )
        if path == "/ajax/media-library/search":
            body = json.loads(request.content)
            self.search_calls.append(body)
            assert body["limit"] <= 100
            library = self.libraries[host]
            skip, limit = int(body["skip"]), int(body["limit"])
            page = library[skip : skip + limit]
            return httpx.Response(200, json={"total": len(library), "media_libraries": page})
        if path == "/ajax/media-library/upload":
            body = json.loads(request.content)
            self.uploads.append({"host": host, "headers": dict(request.headers), "body": body})
            if self.upload_status >= 400:
                return httpx.Response(
                    self.upload_status, json={"error": {"code": "nope", "message": "nope"}}
                )
            self.uploaded += 1
            media_id = f"prod-media-{self.uploaded}"
            self.libraries["prod.test"].append(
                _item(media_id, name=body["filename"], media_format=body["media_library_format"])
            )
            return httpx.Response(
                self.upload_status,
                json={"media_library": {"id": media_id, "name": body["filename"]}},
            )
        return httpx.Response(404, json={"error": {"code": "not_found", "message": path}})


@pytest.fixture(autouse=True)
def _clean_jobs():
    service.reset_jobs()
    yield
    service.reset_jobs()


@pytest.fixture
def db():
    return mongomock.MongoClient().db


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        storage_dir=str(tmp_path / "storage"),
        am_bypass_local=True,
        social_media_origin="http://127.0.0.1:5173",
        am_base_url="https://am.connexup-uat.net",
        am_upload_max_body_bytes=9_500_000,
    )


def _payload(**overrides) -> dict:
    payload = {
        "uat_base_url": "https://uat.test",
        "uat_merchant_id": "m-uat",
        "uat_cookie": COOKIE,
        "prod_base_url": "https://prod.test",
        "prod_merchant_id": "m-prod",
        "prod_cookie": PROD_COOKIE,
        "skip_same_name": True,
        "force": False,
    }
    payload.update(overrides)
    return payload


_console: FakeAMConsole | None = None


def _console_for(**kwargs) -> FakeAMConsole:
    global _console
    _console = FakeAMConsole(**kwargs)
    return _console


def _wait(db, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.get_job(db, job_id)
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not settle in {timeout}s")


def _dump_database(db) -> str:
    """Everything this tool wrote, as text: the sessions must not appear anywhere."""
    return json.dumps(
        {name: list(db[name].find()) for name in db.list_collection_names()},
        default=str,
        ensure_ascii=False,
    )


# --- gate -----------------------------------------------------------------


def _api_client(settings: Settings, db, *, transport: httpx.BaseTransport | None = None) -> TestClient:
    app = FastAPI()

    @app.exception_handler(MediaMigrationError)
    async def _handler(_: Request, exc: MediaMigrationError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.state.settings = settings
    app.state.raw_db = db
    app.state.media_migration_transport = transport
    app.include_router(migration_routes.router)
    return TestClient(app)


def _csrf() -> dict[str, str]:
    return {"Origin": "http://127.0.0.1:5173", "X-Media-Request": "1"}


def test_routes_are_hidden_without_local_bypass(settings, db):
    settings.am_bypass_local = False
    client = _api_client(settings, db)
    read = client.get("/api/media-migration/defaults")
    assert read.status_code == 404
    assert read.json()["error"]["code"] == "media_migration_disabled"
    write = client.post("/api/media-migration/preview", json=_payload(), headers=_csrf())
    assert write.status_code == 404
    assert write.json()["error"]["code"] == "media_migration_disabled"
    jobs = client.get("/api/media-migration/jobs")
    assert jobs.status_code == 404


def test_routes_are_hidden_when_the_origin_is_not_loopback(settings, db):
    settings.social_media_origin = "https://social-media.connexup-uat.net"
    client = _api_client(settings, db)
    assert client.get("/api/media-migration/defaults").status_code == 404


def test_local_write_requires_the_csrf_headers(settings, db):
    console = _console_for()
    client = _api_client(settings, db, transport=console.transport())
    missing = client.post(
        "/api/media-migration/preview", json=_payload(), headers={"Origin": "http://127.0.0.1:5173"}
    )
    assert missing.status_code == 403
    assert missing.json()["error"]["code"] == "csrf_header_required"
    untrusted = client.post(
        "/api/media-migration/preview",
        json=_payload(),
        headers={"Origin": "https://evil.example", "X-Media-Request": "1"},
    )
    assert untrusted.status_code == 403
    assert untrusted.json()["error"]["code"] == "csrf_origin_rejected"
    accepted = client.post("/api/media-migration/preview", json=_payload(), headers=_csrf())
    assert accepted.status_code == 200


def test_defaults_prefill_the_console_urls(settings, db):
    response = _api_client(settings, db).get("/api/media-migration/defaults")
    assert response.status_code == 200
    assert response.json() == {
        "uat_base_url": "https://am.connexup-uat.net",
        "prod_base_url": "https://am.connexup.com",
    }


# --- cookie handling ------------------------------------------------------


def test_session_cookie_normalizes_a_bare_token():
    assert service.session_cookie("abc123") == "AMSessionId=abc123"
    assert service.session_cookie("AMSessionId=abc; other=1") == "AMSessionId=abc; other=1"
    assert service.session_cookie("  ") == ""
    assert service._cookie("token-value", "UAT") == "AMSessionId=token-value"


def test_sessions_are_required():
    with pytest.raises(MediaMigrationError) as exc:
        service.MigrationSpec(_payload(uat_cookie=""))
    assert exc.value.code == "cookie_required"
    with pytest.raises(MediaMigrationError) as exc:
        service.MigrationSpec(_payload(prod_base_url="ftp://prod.test"))
    assert exc.value.code == "invalid_base_url"


# --- preview --------------------------------------------------------------


def test_preview_pages_past_the_hundred_item_limit(settings, db):
    console = _console_for(
        uat=[
            _item(f"u{index:03d}", updated_time=f"2026-05-{1 + index // 60:02d}T00:00:00Z")
            for index in range(250)
        ],
        prod=[],
    )
    result = service.preview(settings, db, _payload(), transport=console.transport())
    assert result["uat_total"] == 250
    assert result["planned"] == 250
    assert len(result["items"]) == 250
    assert {call["skip"] for call in console.search_calls} >= {0, 100, 200}
    assert all(call["media_library_type"] == "IMAGE" for call in console.search_calls)


def test_preview_skips_same_names_and_keeps_tags(settings, db):
    console = _console_for(
        uat=[
            _item("u1", name="招牌红烧肉.jpg", tags=["招牌菜", "热菜", "  热菜 ", ""]),
            _item("u2", name="凉菜拼盘.PNG", media_format="PNG"),
            _item("u3", name="不展示.gif", kind="VIDEO"),
            _item("u4", name="没有地址.jpg", url=""),
        ],
        prod=[_item("p1", name="招牌红烧肉.jpg")],
    )
    result = service.preview(settings, db, _payload(), transport=console.transport())
    assert result["uat_total"] == 2  # non-IMAGE and url-less records are dropped
    assert result["prod_total"] == 1
    statuses = {item["name"]: item["status"] for item in result["items"]}
    assert statuses["招牌红烧肉.jpg"] == "skipped_same_name"
    assert statuses["凉菜拼盘.PNG"] == "planned"
    planned = next(item for item in result["items"] if item["name"] == "凉菜拼盘.PNG")
    assert planned["format"] == "PNG"
    red = next(item for item in result["items"] if item["name"] == "招牌红烧肉.jpg")
    assert red["tags"] == ["招牌菜", "热菜"]
    # A preview writes no checkpoints: nothing ran yet.
    assert db[service.ITEM_COLLECTION].count_documents({}) == 0


def test_preview_reports_which_side_rejected_the_session(settings, db):
    console = _console_for(uat=[_item("u1")])
    console.reject = {"uat.test": 401}
    with pytest.raises(MediaMigrationError) as exc:
        service.preview(settings, db, _payload(), transport=console.transport())
    assert "UAT Cookie 无效或已过期（HTTP 401" in exc.value.message
    assert exc.value.status_code == 401

    console.reject = {"prod.test": 403}
    with pytest.raises(MediaMigrationError) as exc:
        service.preview(settings, db, _payload(), transport=console.transport())
    assert "Prod 拒绝访问该商户（HTTP 403" in exc.value.message
    assert exc.value.status_code == 403


# --- upload preparation ---------------------------------------------------


def test_unsupported_format_is_reencoded_to_jpeg(settings):
    data, media_format, filename = service.prepare_upload(
        _webp(), {"name": "菜品.webp", "format": "WEBP"}, settings
    )
    assert media_format == "JPEG"
    assert filename == "菜品.jpg"
    assert data.startswith(b"\xff\xd8")
    with Image.open(io.BytesIO(data)) as opened:
        assert opened.format == "JPEG"


def test_oversized_image_is_shrunk_below_the_gateway_budget(tmp_path):
    settings = Settings(
        _env_file=None,
        storage_dir=str(tmp_path / "storage"),
        am_upload_max_body_bytes=900_000,
    )
    budget = service.upload_body_max_bytes(settings)
    data, media_format, filename = service.prepare_upload(
        _noisy_jpeg(), {"name": "大图.jpg", "format": "JPEG"}, settings
    )
    assert media_format == "JPEG"
    assert len(data) <= budget
    body = {
        "file_base64": base64.b64encode(data).decode("ascii"),
        "filename": filename,
        "category": "MEDIA_LIBRARY",
    }
    assert len(json.dumps(body).encode()) < settings.am_upload_max_body_bytes


def test_filename_is_rebuilt_with_the_matching_extension(settings):
    data, media_format, filename = service.prepare_upload(
        _jpeg(), {"name": "a/b\\c.jpg", "format": "PNG"}, settings
    )
    assert media_format == "PNG"
    assert filename == "c.png"


# --- full migration -------------------------------------------------------


def test_migration_checkpoints_to_mongodb_without_any_session(settings, db):
    console = _console_for(
        uat=[
            _item(
                "u1",
                name="招牌红烧肉.jpg",
                tags=["招牌菜", "热菜", "热菜", "招牌菜", "宴席", "凉菜", "甜品", "饮品"],
            ),
            _item("u2", name="凉菜.png", media_format="PNG"),
        ],
        prod=[],
    )
    job = service.start_job(settings, db, _payload(), transport=console.transport())
    settled = _wait(db, job["job_id"])
    assert settled["status"] == "succeeded", settled
    assert settled["counters"]["uploaded"] == 2
    assert settled["counters"]["failed"] == 0
    assert len(console.uploads) == 2
    first = console.uploads[0]["body"]
    assert first["category"] == "MEDIA_LIBRARY"
    assert first["media_library_type"] == "IMAGE"
    assert first["merchant_id"] == "m-prod"
    # AM rejects more than five tags, duplicates included.
    assert first["tags"] == ["招牌菜", "热菜", "宴席", "凉菜", "甜品"]
    assert base64.b64decode(first["file_base64"]).startswith(b"\xff\xd8")
    # The CDN must never receive the AM session.
    assert console.download_headers
    assert all(entry["cookie"] is None for entry in console.download_headers)

    items = {row["source_media_id"]: row for row in db[service.ITEM_COLLECTION].find({})}
    assert set(items) == {"u1", "u2"}
    saved = items["u1"]
    assert saved["status"] == "uploaded"
    assert saved["prod_media_id"] == "prod-media-1"
    assert saved["uploaded_filename"] == "招牌红烧肉.jpg"
    assert saved["format"] == "JPEG"
    # The checkpoint keeps UAT's whole tag set; only the upload is capped at five.
    assert saved["tags"] == ["招牌菜", "热菜", "宴席", "凉菜", "甜品", "饮品"]
    assert saved["bytes"] > 0
    assert saved["sha256"] == hashlib.sha256(console.default_bytes).hexdigest()
    assert saved["compressed"] is False
    assert saved["last_job_id"] == job["job_id"]
    assert saved["uploaded_at"]
    assert saved["uat_merchant_id"] == "m-uat"
    assert saved["prod_merchant_id"] == "m-prod"

    jobs = list(db[service.JOB_COLLECTION].find({}))
    assert len(jobs) == 1
    assert jobs[0]["_id"] == job["job_id"]
    assert jobs[0]["status"] == "succeeded"
    assert jobs[0]["skip_same_name"] is True
    assert jobs[0]["force"] is False

    # Neither the checkpoints, the job record nor anything else may hold a session.
    dump = _dump_database(db)
    assert "secret-session-value" not in dump
    assert PROD_COOKIE not in dump
    assert COOKIE not in dump

    # A second run is a no-op: the checkpoints already record both uploads.
    replay = service.preview(settings, db, _payload(), transport=console.transport())
    assert replay["skipped_ledger"] == 2
    assert replay["planned"] == 0
    assert replay["items"][0]["prod_media_id"]


def test_checkpoint_is_retried_when_prod_deleted_the_record(settings, db):
    console = _console_for(uat=[_item("u1")], prod=[])
    settled = _wait(
        db, service.start_job(settings, db, _payload(), transport=console.transport())["job_id"]
    )
    assert settled["status"] == "succeeded"
    console.libraries["prod.test"].clear()

    preview = service.preview(settings, db, _payload(), transport=console.transport())
    assert preview["planned"] == 1
    assert preview["skipped_ledger"] == 0
    # The stale Prod id is dropped from the answer instead of being shown as migrated.
    assert preview["items"][0]["prod_media_id"] is None

    forced = service.preview(settings, db, _payload(force=True), transport=console.transport())
    assert forced["planned"] == 1


def test_force_reuploads_even_when_the_checkpoint_matches(settings, db):
    console = _console_for(uat=[_item("u1")], prod=[])
    _wait(db, service.start_job(settings, db, _payload(), transport=console.transport())["job_id"])
    preview = service.preview(settings, db, _payload(force=True), transport=console.transport())
    assert preview["planned"] == 1
    assert preview["skipped_ledger"] == 0


def test_webp_source_is_stored_as_jpeg_in_prod(settings, db):
    console = _console_for(uat=[_item("u1", name="饮品.webp", media_format="WEBP")], prod=[])
    console.files["/u1"] = _webp()
    settled = _wait(
        db, service.start_job(settings, db, _payload(), transport=console.transport())["job_id"]
    )
    assert settled["status"] == "succeeded", settled
    assert settled["counters"]["compressed"] == 1
    body = console.uploads[0]["body"]
    assert body["media_library_format"] == "JPEG"
    assert body["filename"].endswith(".jpg")
    assert base64.b64decode(body["file_base64"]).startswith(b"\xff\xd8")
    checkpoint = db[service.ITEM_COLLECTION].find_one({"source_media_id": "u1"})
    assert checkpoint["format"] == "JPEG"
    assert checkpoint["compressed"] is True
    assert checkpoint["uploaded_filename"] == "饮品.jpg"


def test_a_failed_download_does_not_stop_the_remaining_items(settings, db):
    console = _console_for(uat=[_item("u1"), _item("u2")], prod=[])
    console.files["/u1"] = b""
    settled = _wait(
        db, service.start_job(settings, db, _payload(), transport=console.transport())["job_id"]
    )
    assert settled["status"] == "partial"
    assert settled["counters"]["uploaded"] == 1
    assert settled["counters"]["failed"] == 1
    failed = next(item for item in settled["items"] if item["status"] == "failed")
    assert "下载" in failed["error"]
    # The failure is checkpointed too, so the UI can explain a later re-run.
    failed_row = db[service.ITEM_COLLECTION].find_one({"source_media_id": "u1"})
    assert failed_row["status"] == "failed"
    assert failed_row["prod_media_id"] == ""


def test_rejected_prod_cookie_aborts_the_run_immediately(settings, db):
    console = _console_for(uat=[_item("u1"), _item("u2"), _item("u3")], prod=[])
    console.upload_status = 401
    settled = _wait(
        db, service.start_job(settings, db, _payload(), transport=console.transport())["job_id"]
    )
    assert settled["status"] == "failed"
    assert "Prod Cookie 无效或已过期（HTTP 401" in settled["error"]
    assert len(console.uploads) == 1


def test_limit_only_migrates_the_requested_slice(settings, db):
    console = _console_for(uat=[_item(f"u{index}") for index in range(5)], prod=[])
    settled = _wait(
        db,
        service.start_job(settings, db, _payload(limit=2), transport=console.transport())["job_id"],
    )
    assert settled["limit"] == 2
    assert settled["counters"]["uploaded"] == 2
    assert len(console.uploads) == 2
    assert sum(1 for item in settled["items"] if item["status"] == "planned") == 3
    # Only the uploaded slice is checkpointed; the rest stays planned for the next run.
    assert db[service.ITEM_COLLECTION].count_documents({}) == 2

    # Follow-up full run (limit=0) picks up the remaining 3 without re-uploading the first 2.
    full = _wait(
        db,
        service.start_job(settings, db, _payload(limit=0), transport=console.transport())["job_id"],
    )
    assert full["limit"] == 0
    assert full["counters"]["skipped_ledger"] == 2
    assert full["counters"]["uploaded"] == 3
    assert len(console.uploads) == 5
    assert db[service.ITEM_COLLECTION].count_documents({}) == 5


def test_active_job_is_reused_instead_of_starting_a_second_one(settings, db):
    console = _console_for(uat=[_item(f"u{index}") for index in range(4)], prod=[])
    console.delay = 0.05
    first = service.start_job(settings, db, _payload(), transport=console.transport())
    second = service.start_job(settings, db, _payload(), transport=console.transport())
    assert second["job_id"] == first["job_id"]
    _wait(db, first["job_id"])
    assert db[service.JOB_COLLECTION].count_documents({}) == 1


def test_restart_marks_leftover_jobs_interrupted(settings, db):
    db[service.JOB_COLLECTION].insert_many(
        [
            {"_id": "mmig_running", "status": "running", "counters": {"uploaded": 2}, "current": "x.jpg"},
            {"_id": "mmig_queued", "status": "queued", "counters": {"uploaded": 0}},
            {"_id": "mmig_done", "status": "succeeded", "counters": {"uploaded": 5}},
        ]
    )
    assert service.mark_interrupted(db) == 2
    partial = db[service.JOB_COLLECTION].find_one({"_id": "mmig_running"})
    assert partial["status"] == "partial"
    assert partial["current"] == ""
    assert partial["finished_at"]
    failed = db[service.JOB_COLLECTION].find_one({"_id": "mmig_queued"})
    assert failed["status"] == "failed"
    assert db[service.JOB_COLLECTION].find_one({"_id": "mmig_done"})["status"] == "succeeded"


# --- HTTP job API ---------------------------------------------------------


def test_job_api_runs_cancel_and_reports_progress(settings, db):
    console = _console_for(uat=[_item(f"u{index}") for index in range(6)], prod=[])
    console.delay = 0.05
    client = _api_client(settings, db, transport=console.transport())
    started = client.post("/api/media-migration/jobs", json=_payload(), headers=_csrf())
    assert started.status_code == 200
    job_id = started.json()["job_id"]
    assert started.json()["status"] in {"queued", "running"}
    # The polled snapshot never carries the sessions.
    assert "secret-session-value" not in started.text

    cancelled = client.post(f"/api/media-migration/jobs/{job_id}/cancel", headers=_csrf())
    assert cancelled.status_code == 200

    deadline = time.time() + 20
    job = {}
    while time.time() < deadline:
        job = client.get(f"/api/media-migration/jobs/{job_id}").json()
        if job["status"] not in {"queued", "running"}:
            break
        time.sleep(0.02)
    assert job["status"] == "cancelled"
    assert job["counters"]["uploaded"] < 6
    assert job["finished_at"]

    missing = client.get("/api/media-migration/jobs/mmig_missing")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "job_not_found"


def test_job_history_survives_a_restart(settings, db):
    console = _console_for(uat=[_item("u1"), _item("u2")], prod=[])
    first = service.start_job(settings, db, _payload(), transport=console.transport())
    _wait(db, first["job_id"])
    second = service.start_job(
        settings, db, _payload(force=True), transport=console.transport()
    )
    _wait(db, second["job_id"])

    # A restart drops every in-memory job; the records are still there.
    service.reset_jobs()
    client = _api_client(settings, db)
    history = client.get("/api/media-migration/jobs").json()["jobs"]
    assert [row["job_id"] for row in history] == [second["job_id"], first["job_id"]]
    assert history[0]["counters"]["uploaded"] == 2
    assert "items" not in history[0]
    assert "created_at" in history[0] and history[0]["created_at"].endswith("Z")

    filtered = client.get(
        "/api/media-migration/jobs",
        params={"uat_merchant_id": "m-other", "prod_merchant_id": "m-prod"},
    ).json()["jobs"]
    assert filtered == []

    detail = client.get(f"/api/media-migration/jobs/{first['job_id']}").json()
    assert detail["status"] == "succeeded"
    assert len(detail["items"]) == 2
    assert detail["items"][0]["status"] == "uploaded"

    limited = client.get("/api/media-migration/jobs", params={"limit": 1}).json()["jobs"]
    assert len(limited) == 1
