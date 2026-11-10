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


def _app():
    app = FastAPI()
    raw = mongomock.MongoClient().db
    maintenance.initialize(raw)
    app.state.raw_db = raw
    app.state.settings = Settings(social_media_origin="https://social-media.connexup-uat.net")
    app.state.am = FakeAM()
    app.middleware("http")(merchant_context_middleware)

    @app.get("/api/merchants/{merchant_id}/probe")
    def probe(merchant_id: str, request: Request):
        return {"merchant_id": request.state.merchant_id, "db_merchant": request.state.db.merchant_id}

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


@pytest.mark.parametrize(("method", "path", "expects_profile"), [
    ("POST", "/api/merchants/m1/prompts/optimize", True),
    ("POST", "/api/merchants/m1/whole-image/tasks", True),
    ("GET", "/api/merchants/m1/whole-image/tasks", False),
])
def test_generation_and_optimizer_requests_load_am_profile(method, path, expects_profile):
    app, _ = _app()

    class ProfileAM(FakeAM):
        def profile(self, cookie, merchant_id):
            return {"merchant_id": merchant_id, "brand": {"positioning": "AM_BACKGROUND"}}

    app.state.am = ProfileAM()

    def probe(request: Request):
        return {"profile": request.state.merchant_profile}

    app.add_api_route(path, probe, methods=[method])
    response = TestClient(app).request(method, path, headers={
        "Cookie": "AMSessionId=good", "Origin": "https://social-media.connexup-uat.net", "X-Media-Request": "1",
    })
    assert response.status_code == 200
    profile = response.json()["profile"]
    if expects_profile:
        assert profile["prompt_sections"] == [{"title": "商户定位", "text": "AM_BACKGROUND"}]
    else:
        assert profile is None

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
        "store": "local",
    }


def test_local_bypass_reads_the_stored_profile_for_generation_requests():
    app, _ = _bypass_app()
    body = TestClient(app).get("/api/merchants/local-merchant/brands").json()["profile"]
    assert body["id"] == "local-merchant"
    assert body["prompt_sections"][0] == {"title": "商户定位", "text": "LOCAL_BACKGROUND"}


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
