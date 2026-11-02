from __future__ import annotations

import mongomock
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

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
