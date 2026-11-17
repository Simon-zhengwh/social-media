from __future__ import annotations

from types import SimpleNamespace

import mongomock

from app.config import Settings
from app.main import app
from app.routes import merchant_context
from app.services import maintenance


class FakeAM:
    """Authorization never leaves the imported catalog, so AM has no access hook to call."""

    profile = None


def _request(raw=None, *, context_merchant_id="context-a", bypass=False):
    raw = raw or mongomock.MongoClient().db
    maintenance.initialize(raw)
    settings = Settings(
        social_media_origin="http://127.0.0.1:5173" if bypass else "https://social.example",
        am_bypass_local=bypass,
    )
    query_params = {}
    if context_merchant_id:
        query_params["context_merchant_id"] = context_merchant_id
    am = FakeAM()
    request = SimpleNamespace(
        headers={"cookie": "AMSessionId=valid"},
        query_params=query_params,
        app=SimpleNamespace(state=SimpleNamespace(settings=settings, raw_db=raw, am=am)),
    )
    return request, raw, am


def test_only_the_read_only_imported_catalog_route_is_mounted():
    paths = app.openapi()["paths"]

    assert set(paths["/api/merchants"]) == {"get"}
    assert "/api/merchant-candidates" not in paths


def test_empty_imported_catalog_returns_an_empty_list():
    request, _, _am = _request(context_merchant_id="", bypass=True)

    result = merchant_context.list_merchants(request, keyword="")

    assert result == {"total": 0, "merchants": []}


def test_list_merchants_returns_all_imported_records_without_using_am_as_catalog():
    request, raw, _am = _request()
    raw.merchants.insert_many([
        {"_id": "context-a", "name": "Context A"},
        {"_id": "merchant-b", "name": "Beta", "operators": ["another-user"]},
        {"_id": "merchant-a", "name": "Alpha", "operators": ["operator-a"]},
    ])

    result = merchant_context.list_merchants(request, keyword="")

    assert result == {
        "total": 3,
        "merchants": [
            {"id": "merchant-a", "name": "Alpha"},
            {"id": "merchant-b", "name": "Beta"},
            {"id": "context-a", "name": "Context A"},
        ],
    }


def test_list_merchants_needs_no_am_session_and_no_current_context():
    request, raw, _am = _request(context_merchant_id="")
    request.headers = {}
    raw.merchants.insert_one({"_id": "merchant-a", "name": "Alpha"})

    result = merchant_context.list_merchants(request, keyword="")

    assert result == {"total": 1, "merchants": [{"id": "merchant-a", "name": "Alpha"}]}


def test_list_merchants_searches_only_the_imported_display_name():
    request, raw, _am = _request()
    raw.merchants.insert_many([
        {"_id": "context-a", "name": "Context A"},
        {"_id": "merchant-a", "name": "Keke Food"},
        {"_id": "merchant-b", "name": "Beta"},
    ])

    result = merchant_context.list_merchants(request, keyword=" KEKE ")

    assert result == {"total": 1, "merchants": [{"id": "merchant-a", "name": "Keke Food"}]}


def test_local_bypass_lists_the_same_imported_catalog_without_am_or_config_sources():
    request, raw, _am = _request(context_merchant_id="", bypass=True)
    raw.merchants.insert_many([
        {"_id": "merchant-b", "name": "Beta"},
        {"_id": "merchant-a", "name": "Alpha"},
    ])
    request.app.state.settings = SimpleNamespace(
        social_media_origin="http://127.0.0.1:5173",
        am_bypass_local=True,
        am_bypass_merchant_id="not-imported",
        am_bypass_merchants="config-only=Not Imported",
    )

    result = merchant_context.list_merchants(request, keyword="")

    assert result == {
        "total": 2,
        "merchants": [
            {"id": "merchant-a", "name": "Alpha"},
            {"id": "merchant-b", "name": "Beta"},
        ],
    }
