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


@pytest.mark.parametrize("note", ["", "简洁一些，标题醒目。" * 60], ids=["empty", "long"])
def test_create_notes_only_brand_and_restore_snapshot(note):
    db = mongomock.MongoClient().db
    detail = brands.create_brand(payload(summary="", style_fields=None, note=note), request(db))
    assert detail["note"] == note
    assert detail["style_fields"] is None
    assert detail["summary"] == ""
    assert any(item.id == detail["id"] for item in brand_style.list_brands(db))
    brand = brand_style.get_brand(db, detail["id"])
    restored = brand_style.brand_from_snapshot(brand.snapshot())
    assert restored is not None and restored.note == note


def test_provided_style_fields_still_validate_text():
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
    preview = brands.preview_brand(payload(note="简洁一些"), req)["prompt"]
    assert "备注：简洁一些" in preview and "文案" in preview and "小红书" in preview
    for hidden in ("温暖", "毛笔", "纸感"):
        assert hidden not in preview


@pytest.mark.parametrize("structured", [False, True])
def test_saving_notes_preserves_hidden_brand_data(structured):
    db = mongomock.MongoClient().db
    data = payload().model_dump()
    if not structured:
        data.update(schema_version=1, style_fields=None)
    data.update(_id="test-brand", layout_guide="原布局全文", background_guide="原背景全文",
                legacy_guides={"layout_guide": "归档布局", "background_guide": "归档背景"})
    db.brands.insert_one(data)
    detail = brands.get_brand("test-brand", request(db))
    updated = brands.update_brand("test-brand", BrandUpdate(**{
        **{key: detail[key] for key in BrandUpdate.model_fields if key in detail}, "note": "备注要求",
    }), request(db))
    saved = db.brands.find_one({"_id": "test-brand"})
    for key in ("summary", "style_fields", "layout_guide", "background_guide", "legacy_guides"):
        assert saved[key] == data[key]
    assert updated["note"] == "备注要求"
    clone = brands.duplicate_brand("test-brand", brands.DuplicateBrandRequest(), request(db))
    assert clone["note"] == "备注要求"


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


def test_default_asset_rules_require_original_pixels_and_small_perspective():
    db = mongomock.MongoClient().db
    rules = brand_rules.get_rules(db)
    saved = rules_routes.update_rules(BrandRulesUpdate(**rules), request(db))
    context = brand_rules.rules_context(saved)
    for constraint in ("基于原图", "等比缩放", "画面内旋转", "严格小于 30°", "禁止 30° 及以上",
                       "禁止重绘", "生成式补画或扩图", "不得编造", "摄影场景及其他内容素材"):
        assert constraint in context
    assert saved["asset_usage"] == rules["asset_usage"]


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
    assert "毛笔" not in preview.json()["prompt"]
    assert client.post("/api/brands", json={**data, "note": "x" * 4001}).status_code == 422
    invalid = client.post("/api/brands", json={**data, "platform_id": "missing"})
    assert invalid.status_code == 422
    assert app.state.db.brands.count_documents({}) == 1
    rules = client.get("/api/brand-rules").json()
    assert client.put("/api/brand-rules", json=rules).status_code == 200
    assert client.put("/api/brand-rules", json=rules).status_code == 409
    assert client.put("/api/brand-rules", json={**rules, "copy_accuracy": " "}).status_code == 422
