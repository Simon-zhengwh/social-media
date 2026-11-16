from __future__ import annotations

import mongomock
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from types import SimpleNamespace

from app.config import Settings
from app.middleware import merchant_context_middleware
from app.services import maintenance
from app.services.am_client import AMClientError


class FakeAM:
    def access(self, cookie, merchant_id):
        if cookie != "AMSessionId=good":
            raise AMClientError(401, "unauthorized", "login required")
        return {"user_id": "u1", "user_name": "AM One", "merchant_id": merchant_id, "merchant_name": "Merchant"}

    def profile(self, cookie, merchant_id):
        return {"merchant_id": merchant_id, "brand": {"canonical_name": "Merchant"}, "locations": []}


def _app(imported_merchant_ids=("m1",)):
    app = FastAPI()
    raw = mongomock.MongoClient().db
    if imported_merchant_ids:
        raw.merchants.insert_many(
            {"_id": merchant_id, "name": f"Imported {merchant_id}"}
            for merchant_id in imported_merchant_ids
        )
    maintenance.initialize(raw)
    app.state.raw_db = raw
    app.state.settings = Settings(social_media_origin="https://social-media.connexup-uat.net")
    app.state.am = FakeAM()
    app.middleware("http")(merchant_context_middleware)

    @app.get("/api/merchants/{merchant_id}/probe")
    def probe(merchant_id: str, request: Request):
        return {
            "merchant_id": request.state.merchant_id,
            "db_merchant": request.state.db.merchant_id,
            "merchant_name": request.state.merchant_context["merchant_name"],
        }

    @app.post("/api/merchants/{merchant_id}/probe")
    def write_probe(merchant_id: str, request: Request):
        request.state.db.assets.insert_one({"_id": "a", "name": merchant_id})
        return {"ok": True}

    return app, raw


def test_middleware_requires_am_session_and_csrf_for_writes():
    app, raw = _app()
    client = TestClient(app)
    assert client.get("/api/merchants/m1/probe").status_code == 401
    assert client.get("/api/merchants/m1/probe", headers={"Cookie": "AMSessionId=good"}).json()["merchant_id"] == "m1"
    assert client.post("/api/merchants/m1/probe", headers={"Cookie": "AMSessionId=good"}).status_code == 403
    assert client.post(
        "/api/merchants/m1/probe",
        headers={"Cookie": "AMSessionId=good", "Origin": "https://evil.connexup-uat.net", "X-Media-Request": "1"},
    ).status_code == 403
    assert client.post(
        "/api/merchants/m1/probe",
        headers={"Cookie": "AMSessionId=good", "Origin": "https://social-media.connexup-uat.net"},
    ).status_code == 403
    assert client.post(
        "/api/merchants/m1/probe",
        headers={
            "Cookie": "AMSessionId=good",
            "Origin": "https://social-media.connexup-uat.net",
            "X-Media-Request": "1",
        },
    ).status_code == 200
    assert raw.assets.find_one({"_id": "a"})["merchant_id"] == "m1"


def test_uat_merchant_name_comes_from_the_imported_catalog():
    app, _ = _app()

    response = TestClient(app).get(
        "/api/merchants/m1/probe",
        headers={"Cookie": "AMSessionId=good"},
    )

    assert response.status_code == 200
    assert response.json()["merchant_name"] == "Imported m1"


def test_profileless_merchant_can_reach_asset_routes_without_loading_a_brand_profile():
    app, _ = _app()

    class ProfilelessAM(FakeAM):
        def profile(self, cookie, merchant_id):
            raise AssertionError("asset requests must not require a brand profile")

    app.state.am = ProfilelessAM()

    @app.post("/api/merchants/m1/assets")
    def asset_probe(request: Request):
        return {"profile": request.state.merchant_profile}

    response = TestClient(app).post(
        "/api/merchants/m1/assets",
        headers={
            "Cookie": "AMSessionId=good",
            "Origin": "https://social-media.connexup-uat.net",
            "X-Media-Request": "1",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"profile": None}


def test_unimported_am_merchant_cannot_be_opened_directly():
    app, raw = _app(imported_merchant_ids=())
    client = TestClient(app)

    response = client.get(
        "/api/merchants/m1/probe",
        headers={"Cookie": "AMSessionId=good"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "merchant_not_imported"
    assert raw.merchants.count_documents({"_id": "m1"}) == 0


def test_context_does_not_request_a_restaurant_profile():
    from app.routes.merchant_context import router

    app, _ = _app()

    class ProfilelessAM(FakeAM):
        def profile(self, cookie, merchant_id):
            raise AssertionError("context must not request restaurant profiles")

    app.state.am = ProfilelessAM()
    app.include_router(router, prefix="/api/merchants/{merchant_id}")
    response = TestClient(app).get(
        "/api/merchants/m1/context", headers={"Cookie": "AMSessionId=good"},
    )
    assert response.status_code == 200
    assert response.json()["profile"] is None
    assert response.json()["merchant"]["merchant_name"] == "Imported m1"


def test_middleware_returns_503_when_maintenance_enabled():
    app, raw = _app()
    maintenance.enable(raw)
    response = TestClient(app).post(
        "/api/merchants/m1/probe",
        headers={
            "Cookie": "AMSessionId=good",
            "Origin": "https://social-media.connexup-uat.net",
            "X-Media-Request": "1",
        },
    )
    assert response.status_code == 503
    assert raw.assets.count_documents({}) == 0


@pytest.mark.parametrize(("method", "path"), [
    ("POST", "/api/merchants/m1/prompts/optimize"),
    ("POST", "/api/merchants/m1/whole-image/tasks"),
    ("GET", "/api/merchants/m1/whole-image/tasks"),
    ("GET", "/api/merchants/m1/brands"),
])
def test_merchant_requests_do_not_load_restaurant_profiles(method, path):
    app, _ = _app()

    class ProfileAM(FakeAM):
        def profile(self, cookie, merchant_id):
            raise AssertionError("restaurant profiles are unrelated to social-media")

    app.state.am = ProfileAM()

    def probe(request: Request):
        return {"profile": request.state.merchant_profile}

    app.add_api_route(path, probe, methods=[method])
    response = TestClient(app).request(method, path, headers={
        "Cookie": "AMSessionId=good", "Origin": "https://social-media.connexup-uat.net", "X-Media-Request": "1",
    })
    assert response.status_code == 200
    assert response.json()["profile"] is None

def _bypass_app(origin: str = "http://127.0.0.1:5173", bypass: bool = True, merchant: str = "local-merchant"):
    app = FastAPI()
    raw = mongomock.MongoClient().db
    maintenance.initialize(raw)
    raw.merchant_profiles.insert_one(
        {
            "_id": merchant,
            "source": {
                "brand": {"canonical_name": "SEE JOY", "positioning": "LOCAL_BACKGROUND"},
                "locations": [{"place_id": "p1", "name": "One", "core_highlights": ["hot pot"]}],
            },
        }
    )
    raw.merchants.insert_one({"_id": merchant, "name": "SEE JOY喜聚"})
    app.state.raw_db = raw
    app.state.settings = Settings(
        social_media_origin=origin,
        am_bypass_local=bypass,
        am_bypass_merchant_id=merchant,
    )
    app.state.am = FakeAM()
    app.state.local_store = SimpleNamespace(name="local")
    app.middleware("http")(merchant_context_middleware)

    @app.get("/api/merchants/{merchant_id}/probe")
    def probe(merchant_id: str, request: Request):
        return {
            "merchant_id": request.state.merchant_id,
            "user_id": request.state.merchant_context["user_id"],
            "merchant_name": request.state.merchant_context["merchant_name"],
            "store": request.state.store.name,
        }

    @app.get("/api/merchants/{merchant_id}/brands")
    def brands(merchant_id: str, request: Request):
        return {"profile": request.state.merchant_profile}

    @app.post("/api/merchants/{merchant_id}/probe")
    def write_probe(merchant_id: str, request: Request):
        return {"ok": True}

    return app, raw


def test_local_bypass_serves_context_and_local_store_without_a_cookie():
    app, _ = _bypass_app()
    client = TestClient(app)
    response = client.get("/api/merchants/local-merchant/probe")
    assert response.status_code == 200
    assert response.json() == {
        "merchant_id": "local-merchant",
        "user_id": "local-dev",
        "merchant_name": "SEE JOY喜聚",
        "store": "local",
    }


def test_local_bypass_uses_the_imported_merchant_name():
    app, raw = _bypass_app()
    raw.merchants.update_one(
        {"_id": "local-merchant"}, {"$set": {"name": "Imported local name"}}
    )

    response = TestClient(app).get("/api/merchants/local-merchant/probe")

    assert response.status_code == 200
    assert response.json()["merchant_name"] == "Imported local name"


def test_local_bypass_uses_only_imported_merchants_and_rejects_unknown_ids():
    app, raw = _bypass_app()
    raw.merchants.insert_many([
        {"_id": "keke", "name": "Keke Food可可小卤"},
        {"_id": "szechuan", "name": "Szechuan Gourmet"},
    ])
    client = TestClient(app)
    assert client.get("/api/merchants/local-merchant/probe").json()["merchant_name"] == "SEE JOY喜聚"
    assert client.get("/api/merchants/keke/probe").json()["merchant_name"] == "Keke Food可可小卤"
    assert client.get("/api/merchants/szechuan/probe").json()["merchant_name"] == "Szechuan Gourmet"
    assert client.get("/api/merchants/unknown/probe").status_code == 404


def test_local_bypass_does_not_read_stored_restaurant_profiles():
    app, _ = _bypass_app()
    body = TestClient(app).get("/api/merchants/local-merchant/brands").json()["profile"]
    assert body is None


def test_local_bypass_allows_the_vite_origin_for_writes():
    app, _ = _bypass_app()
    client = TestClient(app)
    ok = client.post(
        "/api/merchants/local-merchant/probe",
        headers={"Origin": "http://127.0.0.1:5173", "X-Media-Request": "1"},
    )
    assert ok.status_code == 200
    assert client.post("/api/merchants/local-merchant/probe", headers={"Origin": "http://evil.test", "X-Media-Request": "1"}).status_code == 403


def test_local_bypass_is_ignored_when_am_is_disabled_or_the_origin_is_public():
    app, _ = _bypass_app(bypass=False)
    assert TestClient(app).get("/api/merchants/local-merchant/probe").status_code == 401

    public, _ = _bypass_app(origin="https://social-media.connexup-uat.net")
    assert TestClient(public).get("/api/merchants/local-merchant/probe").status_code == 401
