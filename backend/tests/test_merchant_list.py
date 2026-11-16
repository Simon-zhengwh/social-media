from __future__ import annotations

from types import SimpleNamespace

import httpx
import mongomock

from app.config import Settings
from app.routes import merchant_context
from app.services.am_client import AMClient


def _client(handler) -> AMClient:
    settings = Settings(am_base_url="https://am.test", am_timeout_seconds=5)
    return AMClient(settings, http_client=httpx.Client(transport=httpx.MockTransport(handler)))


class _ExplodingAM:
    def access(self, cookie, merchant_id):
        raise AssertionError("AM access must be bypassed locally")

    def search_media(self, cookie, merchant_id, **kwargs):
        raise AssertionError("AM media must not be called under the local bypass")


def _deployed_request():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "merchant-a", "name": "Alpha", "brand": {}})

    settings = Settings(social_media_origin="https://social.example", am_bypass_local=False)
    request = SimpleNamespace(
        headers={"cookie": "AMSessionId=abc"},
        query_params={"merchant_id": "merchant-a"},
        app=SimpleNamespace(
            state=SimpleNamespace(settings=settings, am=_client(handler), raw_db=mongomock.MongoClient().db),
        ),
        state=SimpleNamespace(merchant_context={"merchant_id": "merchant-a"}, am_cookie="AMSessionId=abc"),
    )
    return request


def _bypass_request(merchant_id: str, raw):
    settings = Settings(
        social_media_origin="http://127.0.0.1:5173",
        am_bypass_local=True,
        am_bypass_merchant_id=merchant_id,
    )
    request = SimpleNamespace(
        headers={"cookie": ""},
        app=SimpleNamespace(
            state=SimpleNamespace(settings=settings, am=_ExplodingAM(), raw_db=raw, db=raw),
        ),
        state=SimpleNamespace(merchant_context={"merchant_id": merchant_id}, am_cookie=""),
    )
    return request


def test_local_bypass_lists_all_imported_merchants():
    raw = mongomock.MongoClient().db
    raw.merchants.insert_many([
        {"_id": "local-merchant", "name": "Local Merchant"},
        {"_id": "keke", "name": "Keke Food可可小卤"},
    ])

    result = merchant_context.list_merchants(
        _bypass_request("local-merchant", raw), keyword=""
    )

    assert result == {
        "total": 2,
        "merchants": [
            {"id": "keke", "name": "Keke Food可可小卤"},
            {"id": "local-merchant", "name": "Local Merchant"},
        ],
    }


def test_assets_route_lists_uploads_without_calling_am_under_the_local_bypass():
    raw = mongomock.MongoClient().db
    raw.assets.insert_one({
        "_id": "asset-1", "merchant_id": "local-merchant", "original_name": "local.png",
        "width": 10, "height": 10, "mime_type": "image/png", "source": "upload",
    })
    request = _bypass_request("local-merchant", raw)
    page = merchant_context.list_am_assets(
        "local-merchant", request, keyword="", skip=0, limit=10,
    )
    assert [item["id"] for item in page["items"]] == ["asset-1"]
    assert page["total"] == 1


def test_context_reports_agent_benchmarks_only_for_the_local_bypass():
    raw = mongomock.MongoClient().db
    bypass = merchant_context.get_merchant_context(
        "local-merchant", _bypass_request("local-merchant", raw)
    )
    assert bypass["features"]["agent_tests"] is True
    assert bypass["features"]["am_media_library"] is False
    assert bypass["features"]["maintenance_cli"] is True

    deployed = merchant_context.get_merchant_context("merchant-a", _deployed_request())
    assert deployed["features"]["agent_tests"] is False
    assert deployed["features"]["am_media_library"] is True
    assert deployed["features"]["maintenance_cli"] is True


def test_local_library_includes_legacy_uploads_but_excludes_outputs_and_other_merchants():
    from app.services.scoped_db import ScopedDatabase

    raw = mongomock.MongoClient().db
    raw.assets.insert_many([
        {"_id": "legacy", "merchant_id": "local-merchant", "original_name": "Dish [1].png"},
        {"_id": "generated", "merchant_id": "local-merchant", "source": "generated", "original_name": "Dish.png"},
        {"_id": "output", "merchant_id": "local-merchant", "kind": "output", "original_name": "Dish.png"},
        {"_id": "other", "merchant_id": "another-merchant", "original_name": "Dish [1].png"},
    ])
    request = _bypass_request("local-merchant", raw)
    request.state.db = ScopedDatabase(raw, "local-merchant")
    page = merchant_context.list_am_assets("local-merchant", request, keyword="[1]", skip=0, limit=10)
    assert page["total"] == 1
    assert [item["id"] for item in page["items"]] == ["legacy"]
    assert page["items"][0]["url"] == "/api/merchants/local-merchant/assets/legacy/file"
