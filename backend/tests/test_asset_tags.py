from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import mongomock
import pytest
from pydantic import ValidationError
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from app.config import Settings
from app.routes import assets as asset_routes
from app.services import am_assets
from app.services.am_client import AMClient, AMClientError
from app.services.scoped_db import ScopedDatabase


def _client(handler) -> AMClient:
    settings = Settings(am_base_url="https://am.example", am_timeout_seconds=3)
    return AMClient(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
    )


def _am_media(media_id: str = "m-1", **extra):
    payload = {
        "id": media_id,
        "name": "dish.png",
        "media_library_format": "PNG",
        "url": "https://cdn.example/dish.png",
        "tags": ["Dish"],
    }
    payload.update(extra)
    return payload


def _upload(data: bytes = b"fake-image-bytes") -> UploadFile:
    return UploadFile(
        file=__import__("io").BytesIO(data),
        filename="dish.png",
        headers=Headers({"content-type": "image/png"}),
    )


def _request(db, settings, *, am=None, store=None):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db=db, settings=settings, am=am, store=store)),
        state=SimpleNamespace(am_cookie="AMSessionId=abc", merchant_id="m1"),
    )


def test_normalize_tags_trims_and_de_duplicates_case_insensitively():
    assert am_assets.normalize_tags([" Dish ", "", "dish", "Hotpot"]) == ["Dish", "Hotpot"]
    assert am_assets.normalize_tags([]) == []
    with pytest.raises(ValueError):
        am_assets.normalize_tags(["a", "b", "c", "d", "e", "f"])


def test_local_search_matches_tag_and_combines_with_the_keyword():
    db = ScopedDatabase(mongomock.MongoClient().db, "m1")
    db.assets.insert_many(
        [
            {"_id": "a", "original_name": "WIDE-DISH.png", "source": "upload", "tags": ["Hotpot", "Dish"]},
            {"_id": "b", "original_name": "WIDE-SOUP.png", "source": "upload", "tags": ["Soup"]},
            {"_id": "c", "original_name": "OTHER.png", "source": "upload"},
        ]
    )

    by_tag = am_assets.list_local(db, "m1", keyword="", tag="hot", skip=0, limit=10)
    assert [item["id"] for item in by_tag["items"]] == ["a"]
    assert by_tag["items"][0]["tags"] == ["Hotpot", "Dish"]

    both = am_assets.list_local(db, "m1", keyword="wide", tag="dish", skip=0, limit=10)
    assert [item["id"] for item in both["items"]] == ["a"]

    # A keyword alone still matches filenames, and an empty filter shows everything.
    assert [item["id"] for item in am_assets.list_local(db, "m1", keyword="soup", tag="", skip=0, limit=10)["items"]] == ["b"]
    assert am_assets.list_local(db, "m1", keyword="", tag="", skip=0, limit=10)["total"] == 3


def test_am_search_passes_the_tag_filter():
    seen = []

    def handler(request: httpx.Request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"media_libraries": [_am_media()], "total": 1})

    page = am_assets.list_media(
        _client(handler), "AMSessionId=abc", "m1", keyword="", tag="hotpot", skip=0, limit=10
    )

    assert seen[0]["tag"] == "hotpot"
    assert page["items"][0]["tags"] == ["Dish"]


def test_local_tag_update_never_calls_am(settings):
    def handler(request: httpx.Request):
        raise AssertionError("local mode must not call AM")

    db = mongomock.MongoClient().db
    db.assets.insert_one({"_id": "local", "original_name": "dish.png", "source": "upload"})

    updated = am_assets.update_tags(db, None, "", "m1", "local", ["  Hotpot ", "dish"])

    assert updated["tags"] == ["Hotpot", "dish"]
    assert db.assets.find_one({"_id": "local"})["tags"] == ["Hotpot", "dish"]
    assert not db.media_objects.count_documents({})


def test_registered_upload_keeps_am_tags_authoritative():
    seen = []

    def handler(request: httpx.Request):
        seen.append((request.method, request.url.path))
        if request.method == "PUT":
            return httpx.Response(200, json={"media_library": _am_media("m-1", tags=["Hotpot"])})
        return httpx.Response(404)

    db = mongomock.MongoClient().db
    db.assets.insert_one(
        {"_id": "local", "original_name": "dish.png", "source": "upload", "am_media_library_id": "m-1"}
    )

    updated = am_assets.update_tags(
        db, _client(handler), "AMSessionId=abc", "m1", "local", ["Hotpot"]
    )

    assert seen == [("PUT", "/ajax/media-library/m-1/tags")]
    assert updated["id"] == "local"
    assert updated["tags"] == ["Hotpot"]
    assert db.assets.find_one({"_id": "local"})["tags"] == ["Hotpot"]


def test_combined_page_lists_registered_uploads_once_with_the_internal_id():
    def handler(request: httpx.Request):
        return httpx.Response(
            200, json={"media_libraries": [_am_media("m-1")], "total": 1}
        )

    raw = mongomock.MongoClient().db
    raw.assets.insert_many(
        [
            {
                "_id": "registered",
                "merchant_id": "m1",
                "source": "upload",
                "original_name": "dish.png",
                "width": 320,
                "height": 240,
                "am_media_library_id": "m-1",
            },
            {"_id": "legacy", "merchant_id": "m1", "source": "upload", "original_name": "legacy.png"},
        ]
    )
    db = ScopedDatabase(raw, "m1")

    page = am_assets.list_for_merchant(
        db, _client(handler), "AMSessionId=abc", "m1", keyword="", tag="", skip=0, limit=10
    )

    # The registered upload arrives from AM only; the legacy upload stays a local row.
    assert page["total"] == 2
    assert [item["id"] for item in page["items"]] == ["legacy", "registered"]
    assert page["items"][1]["am_media_library_id"] == "m-1"
    assert page["items"][1]["read_only"] is True


def test_upload_registers_the_media_library_record_and_remembers_the_mapping(settings, store, product_png):
    seen = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={"media_library": _am_media("m-9")})

    am = _client(handler)
    db = ScopedDatabase(mongomock.MongoClient().db, "m1")
    request = _request(db, settings, am=am, store=store)

    uploaded = asset_routes.upload_asset(request, _upload(product_png))

    assert seen[0]["merchant_id"] == "m1"
    assert seen[0]["filename"] == "dish.png"
    assert base64.b64decode(seen[0]["file_base64"]) == product_png
    assert seen[0]["media_library_type"] == "IMAGE"
    assert seen[0]["category"] == "MEDIA_LIBRARY"
    stored = db.assets.find_one({"_id": uploaded["id"]})
    assert stored["am_media_library_id"] == "m-9"


def test_upload_failure_is_reported_and_leaves_no_private_copy(settings, store, product_png):
    def handler(request: httpx.Request):
        return httpx.Response(403, json={"error": {"code": "MERCHANT_ACCESS_DENIED", "message": "无权上传"}})

    db = ScopedDatabase(mongomock.MongoClient().db, "m1")
    request = _request(db, settings, am=_client(handler), store=store)

    with pytest.raises(HTTPException) as exc:
        asset_routes.upload_asset(request, _upload(product_png))

    assert exc.value.status_code == 403
    assert db.assets.count_documents({}) == 0


def test_tag_route_rejects_too_many_tags(settings):
    db = ScopedDatabase(mongomock.MongoClient().db, "m1")
    db.assets.insert_one({"_id": "local", "original_name": "dish.png", "source": "upload", "created_at": datetime.now(timezone.utc)})

    # The request model rejects six tags before the service ever sees them.
    with pytest.raises(ValidationError):
        asset_routes.AssetTagsUpdate(tags=[f"t{index}" for index in range(6)])

    with pytest.raises(HTTPException) as exc:
        asset_routes.update_asset_tags(
            "local",
            asset_routes.AssetTagsUpdate.model_construct(tags=[f"t{index}" for index in range(6)]),
            _request(db, settings),
        )
    assert exc.value.status_code == 422

    cleared = asset_routes.update_asset_tags(
        "local", asset_routes.AssetTagsUpdate(tags=[]), _request(db, settings)
    )
    assert cleared["tags"] == []


def test_tag_route_reports_am_permission_errors_without_local_fallback(settings):
    def handler(request: httpx.Request):
        return httpx.Response(403, json={"error": {"code": "MERCHANT_ACCESS_DENIED", "message": "无权修改"}})

    db = ScopedDatabase(mongomock.MongoClient().db, "m1")
    db.assets.insert_one(
        {"_id": "local", "original_name": "dish.png", "source": "upload", "am_media_library_id": "m-1"}
    )

    with pytest.raises(HTTPException) as exc:
        asset_routes.update_asset_tags(
            "local", asset_routes.AssetTagsUpdate(tags=["Dish"]), _request(db, settings, am=_client(handler))
        )

    assert exc.value.status_code == 403
    assert "tags" not in db.assets.find_one({"_id": "local"})
