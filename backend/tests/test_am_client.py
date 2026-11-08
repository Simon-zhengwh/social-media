from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
import mongomock

from app.services import am_assets
from app.services.am_client import AMClient, AMClientError
from app.services.scoped_db import ScopedDatabase


def client_with(handler):
    settings = Settings(
        am_base_url="https://am.example",
        am_media_storage_service_key="service-secret",
        am_timeout_seconds=3,
    )
    return AMClient(settings, http_client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True))


def test_access_profile_and_media_use_employee_cookie_and_merchant_scope():
    seen = []

    def handler(request: httpx.Request):
        seen.append((request.method, request.url.path, request.headers.get("cookie"), request.content))
        if request.url.path.endswith("/access"):
            return httpx.Response(200, json={"user_id": "u1", "merchant_id": "m1"})
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"merchant_id": "m1", "brand": {}})
        if request.url.path.endswith("/search"):
            body = json.loads(request.content)
            assert body["merchant_id"] == "m1"
            assert body["filename"] == "ume"
            return httpx.Response(200, json={"media_libraries": [], "total": 0})
        return httpx.Response(404)

    am = client_with(handler)
    assert am.access("AMSessionId=abc", "m1")["user_id"] == "u1"
    assert am.profile("AMSessionId=abc", "m1")["merchant_id"] == "m1"
    assert am.search_media("AMSessionId=abc", "m1", keyword="ume")["total"] == 0
    assert all(item[2] == "AMSessionId=abc" for item in seen)


def test_service_media_calls_use_bearer_and_never_cookie():
    seen = []

    def handler(request: httpx.Request):
        seen.append((request.url.path, request.headers.get("authorization"), request.headers.get("cookie")))
        if request.url.path.endswith("init-upload"):
            return httpx.Response(201, json={"file_key": "/fbr-uat/product/m1/media/originals/u/a.png", "upload_url": "https://blob.example/sas", "expires_at": "2026-11-02T00:00:00Z"})
        return httpx.Response(200, json={"file_key": "/fbr-uat/product/m1/media/originals/u/a.png", "url": "https://cdn.example/a.png", "size": 2})

    am = client_with(handler)
    ticket = am.initiate_media_upload(merchant_id="m1", kind="ORIGINALS", filename="a.png")
    completed = am.complete_media_upload(merchant_id="m1", kind="ORIGINALS", file_key=ticket.file_key)
    assert ticket.upload_url == "https://blob.example/sas"
    assert completed["size"] == 2
    assert seen == [
        ("/internal/media/files/init-upload", "Bearer service-secret", None),
        ("/internal/media/files/complete-upload", "Bearer service-secret", None),
    ]


def test_missing_profile_is_none_but_forbidden_is_error():
    def handler(request: httpx.Request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(404, json={"error": {"code": "RESTAURANT_BRAND_PROFILE_NOT_FOUND"}})
        return httpx.Response(403, json={"error": {"code": "MERCHANT_ACCESS_DENIED"}})

    am = client_with(handler)
    assert am.profile("cookie", "m1") is None
    with pytest.raises(AMClientError) as exc:
        am.access("cookie", "m1")
    assert exc.value.status_code == 403
    assert exc.value.code == "MERCHANT_ACCESS_DENIED"

def test_combined_asset_page_includes_scoped_uploads_before_am_media():
    raw = mongomock.MongoClient().db
    now = "2026-11-02T00:00:00Z"
    raw.assets.insert_one({
        "_id": "local-a", "merchant_id": "m1", "source": "upload",
        "original_name": "WIDE.png", "width": 2400, "height": 1600,
        "mime_type": "image/png", "prepared_url": "https://media.example/WIDE.png",
        "created_at": now,
    })
    raw.assets.insert_one({
        "_id": "local-b", "merchant_id": "m2", "source": "upload",
        "original_name": "OTHER.png", "width": 10, "height": 10,
        "mime_type": "image/png", "created_at": now,
    })

    def handler(request: httpx.Request):
        assert request.url.path.endswith("/search")
        return httpx.Response(200, json={
            "media_libraries": [{"id": "am-1", "name": "AM.png", "media_library_format": "PNG"}],
            "total": 1,
        })

    page = am_assets.list_for_merchant(
        ScopedDatabase(raw, "m1"),
        client_with(handler),
        "AMSessionId=abc",
        "m1",
        keyword="",
        skip=0,
        limit=10,
    )

    assert page["total"] == 2
    assert [item["source"] for item in page["items"]] == ["upload", "am"]
    assert page["items"][0]["original_name"] == "WIDE.png"
    assert page["items"][0]["width"] == 2400
    assert page["items"][1]["id"] == "am:am-1"