from __future__ import annotations

from types import SimpleNamespace

import mongomock
import pytest
from fastapi import HTTPException

from app.config import Settings
from app.main import app
from app.routes import merchant_context
from app.services import maintenance
from app.services.am_client import AMClientError


class FakeAM:
    def __init__(self):
        self.access_calls = []

    def access(self, cookie, merchant_id):
        self.access_calls.append((cookie, merchant_id))
        if cookie != "AMSessionId=valid":
            raise AMClientError(401, "unauthorized", "login required")
        return {
            "user_id": "operator-a",
            "merchant_id": merchant_id,
            "merchant_name": "AM-owned display name",
        }


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
    request, _, am = _request(context_merchant_id="", bypass=True)

    result = merchant_context.list_merchants(request, keyword="")

    assert result == {"total": 0, "merchants": []}
    assert am.access_calls == []


def test_list_merchants_returns_all_imported_records_without_using_am_as_catalog():
    request, raw, am = _request()
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
    assert am.access_calls == [("AMSessionId=valid", "context-a")]


def test_list_merchants_searches_only_the_imported_display_name():
    request, raw, _ = _request()
    raw.merchants.insert_many([
        {"_id": "context-a", "name": "Context A"},
        {"_id": "merchant-a", "name": "Keke Food"},
        {"_id": "merchant-b", "name": "Beta"},
    ])

    result = merchant_context.list_merchants(request, keyword=" KEKE ")

    assert result == {"total": 1, "merchants": [{"id": "merchant-a", "name": "Keke Food"}]}


def test_list_merchants_requires_an_imported_current_context_and_am_session():
    request, raw, am = _request(context_merchant_id="not-imported")
    raw.merchants.insert_one({"_id": "merchant-a", "name": "Alpha"})

    with pytest.raises(HTTPException) as exc:
        merchant_context.list_merchants(request, keyword="")

    assert exc.value.status_code == 404
    assert am.access_calls == []


def test_list_merchants_rejects_a_missing_am_context():
    request, raw, _ = _request(context_merchant_id="")
    raw.merchants.insert_one({"_id": "merchant-a", "name": "Alpha"})

    with pytest.raises(HTTPException) as exc:
        merchant_context.list_merchants(request, keyword="")

    assert exc.value.status_code == 400


def test_local_bypass_lists_the_same_imported_catalog_without_am_or_config_sources():
    request, raw, am = _request(context_merchant_id="", bypass=True)
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
    assert am.access_calls == []
