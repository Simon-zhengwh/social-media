from __future__ import annotations

import mongomock
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.middleware import merchant_context_middleware
from app.routes import merchant_context
from app.services import maintenance
from app.services.object_store import LocalObjectStore


class FakeAM:
    def access(self, cookie, merchant_id):
        return {"user_id": "u1", "user_name": "AM", "merchant_id": merchant_id, "merchant_name": "SEE JOY"}

    def profile(self, cookie, merchant_id):
        return {"merchant_id": merchant_id, "brand": {"canonical_name": "SEE JOY"}, "locations": []}


def _client(tmp_path, *, local: bool) -> TestClient:
    raw = mongomock.MongoClient().db
    maintenance.initialize(raw)
    settings = Settings(
        storage_dir=str(tmp_path / "storage"),
        am_bypass_local=local,
        am_bypass_merchant_id="m1" if local else "",
        social_media_origin="http://127.0.0.1:5173" if local else "https://social-media.connexup-uat.net",
        am_media_storage_service_key="",
    )
    app = FastAPI()
    app.state.raw_db = raw
    app.state.db = raw
    app.state.settings = settings
    app.state.am = FakeAM()
    app.state.local_store = LocalObjectStore(settings.storage_path / "objects")
    app.middleware("http")(merchant_context_middleware)
    router = APIRouter(prefix="/api/merchants/{merchant_id}")
    router.include_router(merchant_context.router)
    app.include_router(router)
    return TestClient(app)


def test_material_filters_feature_is_off_outside_local_mode(tmp_path):
    client = _client(tmp_path, local=False)
    context = client.get("/api/merchants/m1/context", headers={"Cookie": "AMSessionId=ok"})
    assert context.status_code == 200
    features = context.json()["features"]
    assert features["material_filters"] is False
    assert features["agent_tests"] is False


def test_material_filters_feature_is_on_in_local_mode(tmp_path):
    client = _client(tmp_path, local=True)
    features = client.get("/api/merchants/m1/context").json()["features"]
    assert features["material_filters"] is True
