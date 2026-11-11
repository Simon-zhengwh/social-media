from __future__ import annotations

import io
import time
from pathlib import Path

import mongomock
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.middleware import merchant_context_middleware
from app.routes import material_filters as routes
from app.services import maintenance
from app.services.object_store import LocalObjectStore
from app.services.material_filter_files import MaterialFilterFileError
from app.services.material_filters import MaterialFilterError

ORIGIN = "http://127.0.0.1:5173"
WRITE_HEADERS = {"Origin": ORIGIN, "X-Media-Request": "1"}


def _jpeg(color=(180, 90, 50)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (96, 72), color).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _payload(decision="pass"):
    verdict = {"pass": "pass", "fail": "fail", "uncertain": "uncertain"}[decision]
    return {
        "name": "红烧肉砂锅特写",
        "category": "食物",
        "tags": ["红烧肉", "砂锅", "浓汁", "热菜"],
        "description": "一只砂锅装着红烧肉。",
        "rules": [
            {"id": "empty_seating_scene", "verdict": verdict, "reason": "逐图判断依据。"},
            {"id": "crab_legs", "verdict": "pass", "reason": "未见蟹腿。"},
        ],
        "decision": decision,
    }


class FakeCoreAI:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def chat_json(self, messages, *, model=None, timeout_seconds=None):
        self.calls += 1
        value = self.responses.pop(0) if self.responses else _payload()
        if isinstance(value, Exception):
            raise value
        return value


def _build(monkeypatch, tmp_path, *, local=True):
    raw = mongomock.MongoClient().db
    maintenance.initialize(raw)
    settings = Settings(
        storage_dir=str(tmp_path / "storage"),
        am_bypass_local=local,
        social_media_origin=ORIGIN,
        core_ai_api_key="test-key",
        material_filter_concurrency=2,
    )
    monkeypatch.setattr(
        "app.services.material_filter_files.check_source_readable", lambda _: None
    )
    web = FastAPI()
    web.state.raw_db = raw
    web.state.db = raw
    web.state.settings = settings
    web.state.core_ai = FakeCoreAI()
    web.state.local_store = LocalObjectStore(settings.storage_path / "objects")
    web.middleware("http")(merchant_context_middleware)
    router = APIRouter(prefix="/api/merchants/{merchant_id}")
    router.include_router(routes.router)
    web.include_router(router)

    @web.exception_handler(MaterialFilterError)
    async def _service_error(_, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @web.exception_handler(MaterialFilterFileError)
    async def _file_error(_, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    return web, raw, settings


def _create_folder(client: TestClient, value="\\\\192.168.11.5\\movies\\See Joy素材\\照片"):
    response = client.post(
        "/api/merchants/m1/material-filters/folders",
        json={"input": value},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _point_folder_at(raw, folder_id: str, source: Path) -> None:
    raw.material_filter_folders.update_one(
        {"_id": folder_id}, {"$set": {"source_root": str(source)}}
    )


def _create_scheme(client: TestClient, folder_id: str, **payload):
    body = {"name": "排除空座位和蟹腿"}
    body.update(payload)
    response = client.post(
        f"/api/merchants/m1/material-filters/folders/{folder_id}/schemes",
        json=body,
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _wait(client: TestClient, run_id: str, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/merchants/m1/material-filters/runs/{run_id}").json()
        if run["status"] in {"succeeded", "failed", "interrupted"}:
            return run
        time.sleep(0.05)
    raise AssertionError(f"run did not finish: {run}")


def test_routes_require_local_mode(monkeypatch, tmp_path):
    web, _, _ = _build(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "is_local_bypass", lambda _: False)
    client = TestClient(web)
    response = client.get("/api/merchants/m1/material-filters/folders")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "material_filters_local_only"
    assert client.post(
        "/api/merchants/m1/material-filters/folders",
        json={"input": "\\\\nas\\share"},
        headers=WRITE_HEADERS,
    ).status_code == 403


def test_writes_still_require_csrf_headers(monkeypatch, tmp_path):
    web, _, _ = _build(monkeypatch, tmp_path)
    client = TestClient(web)
    response = client.post(
        "/api/merchants/m1/material-filters/folders", json={"input": "\\\\nas\\share"}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_header_required"
    assert client.post(
        "/api/merchants/m1/material-filters/folders",
        json={"input": "\\\\nas\\share"},
        headers={"Origin": "http://evil.example", "X-Media-Request": "1"},
    ).status_code == 403


def test_folder_deduplication_and_rename(monkeypatch, tmp_path):
    web, raw, _ = _build(monkeypatch, tmp_path)
    client = TestClient(web)
    folder = _create_folder(client)
    assert folder["created"] is True
    duplicate = _create_folder(
        client, "smb://192.168.11.5/movies/See%20Joy%E7%B4%A0%E6%9D%90/%E7%85%A7%E7%89%87"
    )
    assert duplicate["created"] is False
    assert duplicate["id"] == folder["id"]
    assert len(client.get("/api/merchants/m1/material-filters/folders").json()["items"]) == 1

    renamed = client.patch(
        f"/api/merchants/m1/material-filters/folders/{folder['id']}",
        json={"display_name": "See Joy 照片"},
        headers=WRITE_HEADERS,
    )
    assert renamed.status_code == 200
    assert renamed.json()["display_name"] == "See Joy 照片"
    assert renamed.json()["source_root"] == folder["source_root"]


def test_schemes_are_followed_by_folder_and_validated(monkeypatch, tmp_path):
    web, _, _ = _build(monkeypatch, tmp_path)
    client = TestClient(web)
    folder = _create_folder(client)
    scheme = _create_scheme(client, folder["id"])
    assert [rule["id"] for rule in scheme["rules"]] == ["empty_seating_scene", "crab_legs"]
    schemes = client.get(
        f"/api/merchants/m1/material-filters/folders/{folder['id']}/schemes"
    ).json()
    assert [item["id"] for item in schemes["items"]] == [scheme["id"]]

    missing = client.get(
        "/api/merchants/m1/material-filters/folders/mfolder_missing/schemes"
    )
    assert missing.status_code == 404

    invalid_root = client.post(
        f"/api/merchants/m1/material-filters/folders/{folder['id']}/schemes",
        json={"name": "远程输出", "output_root": "\\\\nas\\share\\out"},
        headers=WRITE_HEADERS,
    )
    assert invalid_root.status_code == 422
    assert invalid_root.json()["error"]["code"] == "output_network_path_rejected"

    cloned = client.post(
        f"/api/merchants/m1/material-filters/schemes/{scheme['id']}/clone",
        json={"name": "仅空座位"},
        headers=WRITE_HEADERS,
    )
    assert cloned.status_code == 201
    assert cloned.json()["id"] != scheme["id"]


def test_scan_results_preview_download_export_and_review(monkeypatch, tmp_path):
    web, raw, settings = _build(monkeypatch, tmp_path)
    client = TestClient(web)
    folder = _create_folder(client)
    source = tmp_path / "source"
    source.mkdir()
    (source / "ok.jpg").write_bytes(_jpeg())
    (source / "maybe.jpg").write_bytes(_jpeg((60, 60, 60)))
    _point_folder_at(raw, folder["id"], source)
    scheme = _create_scheme(client, folder["id"])
    raw.material_filter_schemes.update_one(
        {"_id": scheme["id"]}, {"$set": {"source_root_snapshot": str(source)}}
    )
    web.state.core_ai = FakeCoreAI(_payload("pass"), _payload("uncertain"))

    started = client.post(
        f"/api/merchants/m1/material-filters/schemes/{scheme['id']}/runs",
        json={"request_id": "req-1"},
        headers=WRITE_HEADERS,
    )
    assert started.status_code == 202
    run = _wait(client, started.json()["id"])
    assert run["status"] == "succeeded"
    assert run["counters"]["passed"] == 1
    assert run["counters"]["uncertain"] == 1

    duplicate = client.post(
        f"/api/merchants/m1/material-filters/schemes/{scheme['id']}/runs",
        json={"request_id": "req-1"},
        headers=WRITE_HEADERS,
    )
    assert duplicate.json()["id"] == run["id"]

    page = client.get(
        f"/api/merchants/m1/material-filters/schemes/{scheme['id']}/results"
    ).json()
    assert page["total"] == 2
    assert page["counts"]["pass"] == 1
    assert page["counts"]["uncertain"] == 1
    passed = next(item for item in page["items"] if item["effective_decision"] == "pass")
    unsure = next(item for item in page["items"] if item["effective_decision"] == "uncertain")
    assert passed["rules"][0]["verdict"] == "pass"
    assert passed["download_url"]

    preview = client.get(passed["preview_url"])
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/jpeg"

    download = client.get(passed["download_url"])
    assert download.status_code == 200
    assert download.content == (source / passed["original_name"]).read_bytes()

    assert unsure["download_url"] is None

    review = client.post(
        f"/api/merchants/m1/material-filters/evaluations/{unsure['id']}/review",
        json={"decision": "pass", "source_sha256": unsure["source_sha256"], "request_id": "r1"},
        headers=WRITE_HEADERS,
    )
    assert review.status_code == 200
    reviewed = review.json()
    assert reviewed["effective_decision"] == "pass"
    assert reviewed["human_decision"] == "pass"
    assert reviewed["copy_status"] == "copied"
    assert client.get(reviewed["download_url"]).status_code == 200

    again = client.post(
        f"/api/merchants/m1/material-filters/evaluations/{unsure['id']}/review",
        json={"decision": "pass", "source_sha256": unsure["source_sha256"], "request_id": "r1"},
        headers=WRITE_HEADERS,
    )
    assert again.status_code == 200
    assert again.json()["copy_status"] == "copied"

    wide = client.get(
        f"/api/merchants/m1/material-filters/schemes/{scheme['id']}/results",
        params={"limit": 120},
    )
    assert wide.status_code == 200
    too_wide = client.get(
        f"/api/merchants/m1/material-filters/schemes/{scheme['id']}/results",
        params={"limit": 600},
    )
    assert too_wide.status_code == 422

    client.post(
        f"/api/merchants/m1/material-filters/evaluations/{passed['id']}/pick",
        json={"picked": True},
        headers=WRITE_HEADERS,
    )
    exported = client.post(
        f"/api/merchants/m1/material-filters/schemes/{scheme['id']}/export-picked",
        headers=WRITE_HEADERS,
    )
    assert exported.status_code == 200
    exported_body = exported.json()
    assert exported_body["count"] == 1
    assert Path(exported_body["directory"]).is_dir()

    export = client.get(
        f"/api/merchants/m1/material-filters/schemes/{scheme['id']}/export"
    )
    assert export.status_code == 200
    assert "text/csv" in export.headers["content-type"]
    assert "红烧肉砂锅特写" in export.content.decode("utf-8-sig")


def test_metadata_edit_keeps_local_filename(monkeypatch, tmp_path):
    web, raw, _ = _build(monkeypatch, tmp_path)
    client = TestClient(web)
    folder = _create_folder(client)
    source = tmp_path / "source"
    source.mkdir()
    (source / "ok.jpg").write_bytes(_jpeg())
    _point_folder_at(raw, folder["id"], source)
    scheme = _create_scheme(client, folder["id"])
    raw.material_filter_schemes.update_one(
        {"_id": scheme["id"]}, {"$set": {"source_root_snapshot": str(source)}}
    )

    run = client.post(
        f"/api/merchants/m1/material-filters/schemes/{scheme['id']}/runs",
        json={},
        headers=WRITE_HEADERS,
    )
    _wait(client, run.json()["id"])
    item = client.get(
        f"/api/merchants/m1/material-filters/schemes/{scheme['id']}/results"
    ).json()["items"][0]

    updated = client.patch(
        f"/api/merchants/m1/material-filters/evaluations/{item['id']}/metadata",
        json={"display_name": "招牌红烧肉", "tags": ["红烧肉", "招牌菜", "热菜"]},
        headers=WRITE_HEADERS,
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["display_name"] == "招牌红烧肉"
    assert body["output_name"] == item["output_name"]
    assert body["download_url"] == item["download_url"]


def test_review_rejects_a_changed_source_version(monkeypatch, tmp_path):
    web, raw, _ = _build(monkeypatch, tmp_path)
    client = TestClient(web)
    folder = _create_folder(client)
    source = tmp_path / "source"
    source.mkdir()
    (source / "ok.jpg").write_bytes(_jpeg())
    _point_folder_at(raw, folder["id"], source)
    scheme = _create_scheme(client, folder["id"])
    raw.material_filter_schemes.update_one(
        {"_id": scheme["id"]}, {"$set": {"source_root_snapshot": str(source)}}
    )
    web.state.core_ai = FakeCoreAI(_payload("uncertain"))
    run = client.post(
        f"/api/merchants/m1/material-filters/schemes/{scheme['id']}/runs",
        json={},
        headers=WRITE_HEADERS,
    )
    _wait(client, run.json()["id"])
    item = client.get(
        f"/api/merchants/m1/material-filters/schemes/{scheme['id']}/results"
    ).json()["items"][0]

    (source / "ok.jpg").write_bytes(_jpeg((10, 200, 10)))
    response = client.post(
        f"/api/merchants/m1/material-filters/evaluations/{item['id']}/review",
        json={"decision": "pass", "source_sha256": item["source_sha256"]},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] in {"source_changed", "source_version_mismatch"}


def test_folder_records_are_merchant_scoped(monkeypatch, tmp_path):
    web, raw, _ = _build(monkeypatch, tmp_path)
    client = TestClient(web)
    folder = _create_folder(client)
    other = client.get("/api/merchants/m2/material-filters/folders").json()
    assert other["items"] == []
    assert client.patch(
        f"/api/merchants/m2/material-filters/folders/{folder['id']}",
        json={"display_name": "hijack"},
        headers=WRITE_HEADERS,
    ).status_code == 404

def test_manual_pick_endpoints_and_sorts(monkeypatch, tmp_path):
    web, raw, _ = _build(monkeypatch, tmp_path)
    client = TestClient(web)
    folder = _create_folder(client)
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.jpg").write_bytes(_jpeg())
    (source / "b.jpg").write_bytes(_jpeg((20, 140, 60)))
    _point_folder_at(raw, folder["id"], source)
    scheme = _create_scheme(client, folder["id"])
    raw.material_filter_schemes.update_one(
        {"_id": scheme["id"]}, {"$set": {"source_root_snapshot": str(source)}}
    )
    run = client.post(
        f"/api/merchants/m1/material-filters/schemes/{scheme['id']}/runs",
        json={},
        headers=WRITE_HEADERS,
    )
    _wait(client, run.json()["id"])
    items = client.get(
        f"/api/merchants/m1/material-filters/schemes/{scheme['id']}/results",
        params={"sort": "name"},
    ).json()["items"]
    assert [item["original_name"] for item in items] == ["a.jpg", "b.jpg"]
    assert items[0]["manual_pick"] is False

    picked = client.post(
        f"/api/merchants/m1/material-filters/evaluations/{items[0]['id']}/pick",
        json={"picked": True},
        headers=WRITE_HEADERS,
    )
    assert picked.status_code == 200
    assert picked.json()["manual_pick"] is True
    assert picked.json()["effective_decision"] == items[0]["effective_decision"]

    shortlisted = client.get(
        f"/api/merchants/m1/material-filters/schemes/{scheme['id']}/results",
        params={"status": "picked"},
    ).json()
    assert shortlisted["total"] == 1
    assert shortlisted["counts"]["picked"] == 1
    assert shortlisted["items"][0]["id"] == items[0]["id"]

    bulk = client.post(
        f"/api/merchants/m1/material-filters/schemes/{scheme['id']}/picks",
        json={"ids": [items[0]["id"], items[1]["id"]], "picked": True},
        headers=WRITE_HEADERS,
    )
    assert bulk.status_code == 200
    assert bulk.json()["updated"] == 2
    assert client.get(
        f"/api/merchants/m1/material-filters/schemes/{scheme['id']}/results",
        params={"status": "picked"},
    ).json()["total"] == 2

    assert client.get(
        f"/api/merchants/m1/material-filters/schemes/{scheme['id']}/results",
        params={"sort": "bogus"},
    ).status_code == 422

    export = client.get(
        f"/api/merchants/m1/material-filters/schemes/{scheme['id']}/export"
    ).content.decode("utf-8-sig")
    assert "人工入选" in export

def test_delete_folder_endpoint_removes_the_tab(monkeypatch, tmp_path):
    web, raw, _ = _build(monkeypatch, tmp_path)
    client = TestClient(web)
    folder = _create_folder(client)
    other = _create_folder(client, r"\\nas-b\share\照片")
    _create_scheme(client, folder["id"])

    deleted = client.delete(
        f"/api/merchants/m1/material-filters/folders/{folder['id']}",
        headers=WRITE_HEADERS,
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] == folder["id"]
    remaining = client.get("/api/merchants/m1/material-filters/folders").json()["items"]
    assert [item["id"] for item in remaining] == [other["id"]]
    assert client.delete(
        f"/api/merchants/m1/material-filters/folders/{folder['id']}",
        headers=WRITE_HEADERS,
    ).status_code == 404

    assert client.delete(
        f"/api/merchants/m2/material-filters/folders/{other['id']}",
        headers=WRITE_HEADERS,
    ).status_code == 404
