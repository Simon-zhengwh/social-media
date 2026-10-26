from types import SimpleNamespace

import mongomock
import pytest
from fastapi import HTTPException
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.schemas import BrandUpdate, BrandRulesUpdate
from app.routes import brands, brand_rules as rules_routes
from app.services import brand_rules, brand_style


def request(db):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db)))


def payload(**overrides):
    data = dict(name="测试风格", summary="温暖", platform_id="xiaohongshu", placement_id="note",
                schema_version=2, style_fields=dict(palette_notes="深绿", typography="毛笔", subject_style="实拍", background="纸感", layout="清晰"))
    data.update(overrides)
    return BrandUpdate(**data)


def test_v2_required_fields_and_palette():
    with pytest.raises(ValidationError):
        payload(summary="")
    with pytest.raises(ValidationError):
        payload(style_fields=dict(typography="毛笔", subject_style="实拍", background="纸感"))
    with pytest.raises(ValidationError):
        payload(style_fields=dict(palette_notes="深绿", typography=" ", subject_style="实拍", background="纸感"))


def test_create_edit_duplicate_and_preview():
    db = mongomock.MongoClient().db
    req = request(db)
    detail = brands.create_brand(payload(), req)
    assert detail["schema_version"] == 2
    assert detail["style_fields"]["typography"] == "毛笔"
    assert detail["legacy_guides"] is None
    assert "纸感" in detail["layout_guide"]
    saved = brands.update_brand(detail["id"], payload(name="已修改"), req)
    assert saved["name"] == "已修改"
    assert saved["legacy_guides"] is None
    clone = brands.duplicate_brand(detail["id"], brands.DuplicateBrandRequest(), req)
    assert clone["style_fields"] == saved["style_fields"]
    preview = brands.preview_brand(payload(), req)["prompt"]
    assert "毛笔" in preview and "文案" in preview and "小红书" in preview
    assert preview.count("纸感") == 1


def test_rules_read_does_not_write_and_update_is_optimistic():
    db = mongomock.MongoClient().db
    req = request(db)
    rules = brand_rules.get_rules(db)
    assert db.brand_rules.count_documents({}) == 0
    rules["copy_accuracy"] = "准确表达"
    updated = rules_routes.update_rules(BrandRulesUpdate(**rules), req)
    assert updated["version"] == rules["version"] + 1
    with pytest.raises(HTTPException) as exc:
        rules_routes.update_rules(BrandRulesUpdate(**rules), req)
    assert exc.value.status_code == 409


def test_structured_fields_override_old_guides_and_snapshot():
    doc = payload().model_dump()
    doc.update(_id="test-brand", layout_guide="旧内容", background_guide="旧背景")
    brand = brand_style.brand_from_document(doc)
    assert "旧内容" not in brand.layout_guide
    assert brand.snapshot()["style_fields"] == doc["style_fields"]


def test_force_import_cannot_overwrite_structured_brand():
    db = mongomock.MongoClient().db
    db.brands.insert_one({"_id": "seejoy", "schema_version": 2, "name": "保留"})
    result = brand_style._import_loaded_brand(db, None, None, brand_style.default_brand(), force=True)
    assert result["status"] == "skipped"
    assert db.brands.find_one({"_id": "seejoy"})["name"] == "保留"


def test_http_routes_validation_preview_and_common_rules():
    app = FastAPI()
    app.state.db = mongomock.MongoClient().db
    app.include_router(brands.router)
    app.include_router(rules_routes.router)
    client = TestClient(app)
    data = payload().model_dump()
    response = client.post("/api/brands", json=data)
    assert response.status_code == 201
    brand_id = response.json()["id"]
    assert client.get(f"/api/brands/{brand_id}").json()["style_fields"] == data["style_fields"]
    preview = client.post("/api/brands/preview", json=data)
    assert preview.status_code == 200
    assert "毛笔" in preview.json()["prompt"]
    assert client.post("/api/brands", json={**data, "summary": " "}).status_code == 422
    invalid = client.post("/api/brands", json={**data, "platform_id": "missing"})
    assert invalid.status_code == 422
    assert app.state.db.brands.count_documents({}) == 1
    rules = client.get("/api/brand-rules").json()
    assert client.put("/api/brand-rules", json=rules).status_code == 200
    assert client.put("/api/brand-rules", json=rules).status_code == 409
    assert client.put("/api/brand-rules", json={**rules, "copy_accuracy": " "}).status_code == 422
