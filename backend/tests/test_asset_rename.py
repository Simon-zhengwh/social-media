from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import mongomock
import pytest

from app.config import Settings
from app.services import am_assets
from app.services.am_client import AMClient, AMClientError


def _client(handler) -> AMClient:
    settings = Settings(am_base_url="https://am.example", am_timeout_seconds=3)
    return AMClient(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
    )


def _am_media(media_id: str = "m-1", name: str = "dish.png", **extra):
    payload = {
        "id": media_id,
        "name": name,
        "media_library_format": "PNG",
        "url": "https://cdn.example/dish.png",
        "tags": ["Dish"],
    }
    payload.update(extra)
    return payload


def test_rename_media_puts_the_name_and_parses_the_media():
    seen = []

    def handler(request: httpx.Request):
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"media_library": _am_media(name="卤鸡-招牌.png")})

    client = _client(handler)

    media = client.rename_media("AMSessionId=abc", "m1", "m-1", "卤鸡-招牌.png")

    assert seen == [
        ("PUT", "/ajax/media-library/m-1/name", {"merchant_id": "m1", "name": "卤鸡-招牌.png"})
    ]
    assert media["name"] == "卤鸡-招牌.png"


def test_rename_media_rejects_an_invalid_response():
    def handler(request: httpx.Request):
        return httpx.Response(200, json={"media_library": {"id": ""}})

    client = _client(handler)

    with pytest.raises(AMClientError):
        client.rename_media("AMSessionId=abc", "m1", "m-1", "x.png")


def test_normalize_name_trims_and_rejects_blank_or_overlong_names():
    assert am_assets.normalize_name("  卤鸡.png  ") == "卤鸡.png"
    with pytest.raises(ValueError):
        am_assets.normalize_name("   ")
    with pytest.raises(ValueError):
        am_assets.normalize_name("a" * 257 + ".png")
    # The extension is not part of the 256-character stem limit AM applies.
    assert am_assets.normalize_name("a" * 256 + ".png") == "a" * 256 + ".png"


def test_local_rename_writes_the_record_without_calling_am():
    def handler(request: httpx.Request):
        raise AssertionError("local mode must not call AM")

    db = mongomock.MongoClient().db
    db.assets.insert_one({"_id": "local", "original_name": "dish.png", "source": "upload"})

    updated = am_assets.rename_asset(db, None, "", "m1", "local", "  卤鸡.png ")

    assert updated["original_name"] == "卤鸡.png"
    assert updated["id"] == "local"
    assert db.assets.find_one({"_id": "local"})["original_name"] == "卤鸡.png"


def test_rename_of_a_registered_upload_keeps_am_authoritative():
    seen = []

    def handler(request: httpx.Request):
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"media_library": _am_media(name="卤鸡.png")})

    db = mongomock.MongoClient().db
    db.assets.insert_one(
        {
            "_id": "local",
            "original_name": "dish.png",
            "source": "upload",
            "am_media_library_id": "m-1",
        }
    )

    updated = am_assets.rename_asset(
        db, _client(handler), "AMSessionId=abc", "m1", "local", "卤鸡.png"
    )

    assert seen == [
        ("PUT", "/ajax/media-library/m-1/name", {"merchant_id": "m1", "name": "卤鸡.png"})
    ]
    assert updated["id"] == "local"
    assert updated["original_name"] == "卤鸡.png"
    assert db.assets.find_one({"_id": "local"})["original_name"] == "卤鸡.png"


def test_rename_of_an_am_only_asset_uses_the_media_id():
    seen = []

    def handler(request: httpx.Request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"media_library": _am_media(name="卤鸡.png")})

    db = mongomock.MongoClient().db

    updated = am_assets.rename_asset(
        db, _client(handler), "AMSessionId=abc", "m1", "am:m-1", "卤鸡.png"
    )

    assert seen == [("PUT", "/ajax/media-library/m-1/name")]
    assert updated["id"] == "am:m-1"
    assert updated["original_name"] == "卤鸡.png"


def test_rename_of_an_am_only_asset_without_a_session_is_rejected():
    db = mongomock.MongoClient().db

    with pytest.raises(AMClientError):
        am_assets.rename_asset(db, None, "", "m1", "am:m-1", "卤鸡.png")


def test_rename_of_an_unknown_asset_raises_lookup_error():
    db = mongomock.MongoClient().db

    with pytest.raises(LookupError):
        am_assets.rename_asset(db, None, "", "m1", "missing", "卤鸡.png")


def test_am_failure_does_not_change_the_local_name():
    def handler(request: httpx.Request):
        return httpx.Response(500, json={"error": {"code": "boom", "message": "nope"}})

    db = mongomock.MongoClient().db
    db.assets.insert_one(
        {
            "_id": "local",
            "original_name": "dish.png",
            "source": "upload",
            "am_media_library_id": "m-1",
        }
    )

    with pytest.raises(AMClientError):
        am_assets.rename_asset(db, _client(handler), "AMSessionId=abc", "m1", "local", "卤鸡.png")

    assert db.assets.find_one({"_id": "local"})["original_name"] == "dish.png"


def _request(db, *, am=None):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db=db, settings=Settings(), am=am, store=None)),
        state=SimpleNamespace(am_cookie="AMSessionId=abc", merchant_id="m1"),
    )


def test_route_renames_a_local_row_without_an_am_client():
    from app.routes import assets as asset_routes

    db = mongomock.MongoClient().db
    db.assets.insert_one({"_id": "local", "original_name": "dish.png", "source": "upload"})

    payload = asset_routes.AssetNameUpdate(name="卤鸡.png")
    result = asset_routes.update_asset_name("local", payload, _request(db))

    assert result["original_name"] == "卤鸡.png"
    assert db.assets.find_one({"_id": "local"})["original_name"] == "卤鸡.png"


def test_route_rejects_a_blank_name():
    from fastapi import HTTPException

    from app.routes import assets as asset_routes

    db = mongomock.MongoClient().db
    db.assets.insert_one({"_id": "local", "original_name": "dish.png", "source": "upload"})

    with pytest.raises(HTTPException) as error:
        asset_routes.update_asset_name("local", asset_routes.AssetNameUpdate(name="   "), _request(db))

    assert error.value.status_code == 422


def test_route_reports_a_missing_asset():
    from fastapi import HTTPException

    from app.routes import assets as asset_routes

    db = mongomock.MongoClient().db

    with pytest.raises(HTTPException) as error:
        asset_routes.update_asset_name("missing", asset_routes.AssetNameUpdate(name="x.png"), _request(db))

    assert error.value.status_code == 404
