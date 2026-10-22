from __future__ import annotations

import json
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import mongomock
import pytest
from fastapi import BackgroundTasks, HTTPException, UploadFile
from PIL import Image

from app.config import PROJECT_DIR
from app.routes import brands as brand_routes
from app.routes import whole_image as whole_routes
from app.schemas import BrandUpdate, WholeImageTaskCreate
from app.services import brand_style, storage


def _write_skill(
    root: Path,
    folder: str,
    brand_id: str,
    *,
    is_default: bool = False,
    cover: str = "assets/cover.png",
    raw_manifest: str | None = None,
) -> Path:
    skill = root / folder
    (skill / "references").mkdir(parents=True)
    (skill / "assets").mkdir(parents=True)
    (skill / "references" / "layout.md").write_text("LAYOUT GUIDE 测试规范", encoding="utf-8")
    (skill / "references" / "background.md").write_text(
        "BACKGROUND GUIDE 测试背景", encoding="utf-8"
    )
    Image.new("RGB", (40, 60), (12, 34, 56)).save(skill / "assets" / "cover.png")
    manifest = {
        "id": brand_id,
        "name": f"Brand {brand_id}",
        "summary": "test",
        "is_default": is_default,
        "cover": cover,
        "logo_keywords": ["test", "测试"],
        "guides": {
            "layout": "references/layout.md",
            "background": "references/background.md",
        },
    }
    (skill / "brand.json").write_text(
        raw_manifest if raw_manifest is not None else json.dumps(manifest),
        encoding="utf-8",
    )
    return skill


def _import(db, settings, store, skills_dir: Path, **kwargs):
    return brand_style.import_brand_skills(db, store, settings, skills_dir, **kwargs)


def _task_request(db, settings, store):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(db=db, settings=settings, store=store, core_ai=None)
        )
    )


def _seed_product(settings, store, db, product_png):
    prepared = storage.prepare_upload(settings, store, product_png, "dish.png", "product")
    db.assets.insert_one(
        {
            "_id": prepared.id,
            "kind": "product",
            "original_name": prepared.original_name,
            "path": prepared.path,
            "prepared_path": prepared.prepared_path,
            "mime_type": prepared.mime_type,
            "width": prepared.width,
            "height": prepared.height,
            "bytes": prepared.bytes,
            "sha256": prepared.sha256,
            "created_at": datetime.now(timezone.utc),
        }
    )
    return prepared.id


def test_project_skill_imports_three_references(settings, store):
    db = mongomock.MongoClient().db
    result = _import(
        db,
        settings,
        store,
        PROJECT_DIR / ".agents" / "skills",
        force=True,
    )

    assert result["errors"] == []
    assert [item["brand_id"] for item in result["imported"]] == ["seejoy"]
    brand = brand_style.get_brand(db, "seejoy")
    assert brand.name == "SEEJOY 喜聚"
    assert brand.is_default is True
    assert len(brand.reference_asset_ids) == 3
    assert brand.cover_asset_id in brand.reference_asset_ids
    assert "brush" in brand.layout_guide
    assert "不生成任何文字" in brand.background_guide
    assert "skill_dir" not in db.brands.find_one({"_id": "seejoy"})


def test_import_is_idempotent_and_force_updates(tmp_path, settings, store):
    db = mongomock.MongoClient().db
    skill = _write_skill(tmp_path, "custom", "custom", is_default=True)
    first = _import(db, settings, store, tmp_path)
    assert [item["brand_id"] for item in first["imported"]] == ["custom"]
    asset_count = db.assets.count_documents({})

    second = _import(db, settings, store, tmp_path)
    assert second["imported"] == []
    assert [item["brand_id"] for item in second["skipped"]] == ["custom"]
    assert db.assets.count_documents({}) == asset_count

    (skill / "references" / "layout.md").write_text("CHANGED LAYOUT GUIDE", encoding="utf-8")
    third = _import(db, settings, store, tmp_path, force=True)
    assert [item["brand_id"] for item in third["imported"]] == ["custom"]
    assert brand_style.get_brand(db, "custom").layout_guide == "CHANGED LAYOUT GUIDE"


def test_list_brands_reads_mongo_and_orders_default_first(tmp_path, settings, store):
    db = mongomock.MongoClient().db
    _write_skill(tmp_path, "alpha", "alpha")
    _write_skill(tmp_path, "zeta", "zeta", is_default=True)
    _import(db, settings, store, tmp_path)

    brands = brand_style.list_brands(db)
    assert [brand.id for brand in brands] == ["zeta", "alpha"]
    assert brand_style.get_brand(db).id == "zeta"


def test_get_brand_does_not_fallback_to_skill_files(tmp_path, settings, store):
    db = mongomock.MongoClient().db
    _write_skill(tmp_path, "seejoy", "seejoy", is_default=True)

    assert brand_style.list_brands(db) == []
    with pytest.raises(brand_style.BrandError) as exc:
        brand_style.get_brand(db, "seejoy")
    assert exc.value.code == "brand_not_found"


def test_direct_mongo_update_is_visible_without_cache(tmp_path, settings, store):
    db = mongomock.MongoClient().db
    _write_skill(tmp_path, "custom", "custom", is_default=True)
    _import(db, settings, store, tmp_path)

    db.brands.update_one(
        {"_id": "custom"},
        {
            "$set": {
                "layout_guide": "DIRECT MONGO UPDATE",
            },
        },
    )

    brand = brand_style.get_brand(db, "custom")
    assert brand.layout_guide == "DIRECT MONGO UPDATE"


def test_invalid_document_is_skipped_in_list_and_fails_direct_get():
    db = mongomock.MongoClient().db
    db.brands.insert_one(
        {
            "_id": "bad",
            "name": "",
            "layout_guide": "",
            "background_guide": "",
            "is_default": True,
        }
    )

    assert brand_style.list_brands(db) == []
    with pytest.raises(brand_style.BrandError) as exc:
        brand_style.get_brand(db, "bad")
    assert exc.value.code == "invalid_brand"


def test_brand_routes_return_cover_and_options(settings, store):
    db = mongomock.MongoClient().db
    _import(db, settings, store, PROJECT_DIR / ".agents" / "skills")
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(db=db, settings=settings, store=store)
        )
    )

    items = brand_routes.list_brands(request)
    assert items[0]["id"] == "seejoy"
    assert items[0]["cover_url"] == "/api/brands/seejoy/cover"

    cover = brand_routes.brand_cover("seejoy", request)
    image = Image.open(BytesIO(cover.body))
    assert max(image.size) <= 640
    assert cover.headers["cache-control"] == "no-cache"
    with pytest.raises(HTTPException) as exc:
        brand_routes.brand_cover("missing", request)
    assert exc.value.status_code == 404


def test_brand_cover_falls_back_to_first_reference(tmp_path, settings, store):
    db = mongomock.MongoClient().db
    _write_skill(tmp_path, "custom", "custom", is_default=True)
    _import(db, settings, store, tmp_path)
    db.brands.update_one({"_id": "custom"}, {"$set": {"cover_asset_id": None}})
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(db=db, settings=settings, store=store)
        )
    )

    cover = brand_routes.brand_cover("custom", request)
    assert cover.media_type == "image/jpeg"
    assert cover.body


def _brand_request(db, settings, store):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db=db, settings=settings, store=store))
    )


def _update_payload(detail: dict, **overrides) -> BrandUpdate:
    payload = {
        "name": detail["name"],
        "short_name": detail["short_name"],
        "summary": detail["summary"],
        "platform": detail.get("platform", ""),
        "note": detail.get("note", ""),
        "layout_guide": detail["layout_guide"],
        "background_guide": detail["background_guide"],
        "logo_keywords": detail["logo_keywords"],
        "is_default": detail["is_default"],
        "cover_asset_id": detail["cover_asset_id"],
    }
    payload.update(overrides)
    return BrandUpdate(**payload)


def test_brand_detail_returns_guides_and_references(tmp_path, settings, store):
    db = mongomock.MongoClient().db
    _write_skill(tmp_path, "custom", "custom", is_default=True)
    _import(db, settings, store, tmp_path)

    detail = brand_routes.get_brand("custom", _brand_request(db, settings, store))
    assert detail["id"] == "custom"
    assert detail["layout_guide"] == "LAYOUT GUIDE 测试规范"
    assert detail["background_guide"] == "BACKGROUND GUIDE 测试背景"
    assert len(detail["reference_images"]) == 1
    assert detail["reference_images"][0]["is_cover"] is True


def test_update_brand_persists_editable_fields(tmp_path, settings, store):
    db = mongomock.MongoClient().db
    _write_skill(tmp_path, "custom", "custom", is_default=True)
    _import(db, settings, store, tmp_path)
    request = _brand_request(db, settings, store)
    detail = brand_routes.get_brand("custom", request)

    updated = brand_routes.update_brand(
        "custom",
        _update_payload(
            detail,
            name="Updated Brand",
            summary="updated summary",
            layout_guide="NEW LAYOUT",
            background_guide="NEW BACKGROUND",
            logo_keywords=["one", "two"],
        ),
        request,
    )
    assert updated["name"] == "Updated Brand"
    assert updated["layout_guide"] == "NEW LAYOUT"


def test_brand_management_metadata_is_stored_and_listed(tmp_path, settings, store):
    db = mongomock.MongoClient().db
    _write_skill(tmp_path, "custom", "custom", is_default=True)
    _import(db, settings, store, tmp_path)
    request = _brand_request(db, settings, store)
    detail = brand_routes.get_brand("custom", request)

    updated = brand_routes.update_brand(
        "custom",
        _update_payload(
            detail,
            platform="小红书",
            note="优先用于节日促销和门店活动。",
        ),
        request,
    )

    assert updated["platform"] == "小红书"
    assert updated["note"] == "优先用于节日促销和门店活动。"
    listed = brand_routes.list_brands(request)
    assert listed[0]["platform"] == "小红书"
    assert listed[0]["note"] == "优先用于节日促销和门店活动。"
    stored = db.brands.find_one({"_id": "custom"})
    assert stored["platform"] == "小红书"
    assert stored["note"] == "优先用于节日促销和门店活动。"


def test_brand_snapshot_excludes_management_metadata(tmp_path, settings, store):
    db = mongomock.MongoClient().db
    _write_skill(tmp_path, "custom", "custom", is_default=True)
    _import(db, settings, store, tmp_path)
    request = _brand_request(db, settings, store)
    detail = brand_routes.get_brand("custom", request)
    brand_routes.update_brand(
        "custom",
        _update_payload(detail, platform="Instagram", note="仅供运营筛选使用。"),
        request,
    )

    brand = brand_style.get_brand(db, "custom")
    assert brand.platform == "Instagram"
    assert brand.note == "仅供运营筛选使用。"
    snapshot = brand_style.snapshot_brand(brand, db, store, settings)

    assert "platform" not in snapshot
    assert "note" not in snapshot


def _upload_png(color=(240, 120, 20)) -> UploadFile:
    buffer = BytesIO()
    Image.new("RGB", (40, 60), color).save(buffer, format="PNG")
    buffer.seek(0)
    return UploadFile(file=buffer, filename="new-reference.png")


def test_upload_and_delete_brand_reference(tmp_path, settings, store):
    db = mongomock.MongoClient().db
    _write_skill(tmp_path, "custom", "custom", is_default=True)
    _import(db, settings, store, tmp_path)
    request = _brand_request(db, settings, store)
    before = brand_routes.get_brand("custom", request)

    uploaded = brand_routes.upload_reference(
        "custom",
        request,
        _upload_png(),
    )
    assert len(uploaded["reference_images"]) == 2
    new_asset_id = next(
        item["asset_id"]
        for item in uploaded["reference_images"]
        if item["asset_id"] not in {ref["asset_id"] for ref in before["reference_images"]}
    )
    assert db.assets.find_one({"_id": new_asset_id})["kind"] == "brand_reference"

    deleted = brand_routes.delete_reference("custom", new_asset_id, request)
    assert len(deleted["reference_images"]) == 1
    assert db.assets.find_one({"_id": new_asset_id}) is not None


def test_five_brand_references_survive_save_and_task_snapshot(settings, store, product_png):
    db = mongomock.MongoClient().db
    _import(db, settings, store, PROJECT_DIR / ".agents" / "skills")
    request = _brand_request(db, settings, store)
    for count, color in ((4, (10, 20, 30)), (5, (40, 50, 60))):
        detail = brand_routes.upload_reference("seejoy", request, _upload_png(color))
        assert len(detail["reference_images"]) == count
    ids = [item["asset_id"] for item in detail["reference_images"]]
    saved = brand_routes.update_brand(
        "seejoy", _update_payload(detail, cover_asset_id=ids[-1]), request,
    )
    assert saved["cover_asset_id"] == ids[-1]
    assert [item["asset_id"] for item in saved["reference_images"]] == ids

    product_id = _seed_product(settings, store, db, product_png)
    created = whole_routes.create_task(
        WholeImageTaskCreate(prompt="test", product_asset_ids=[product_id], brand_mode="preset", brand_id="seejoy"),
        _task_request(db, settings, store), BackgroundTasks(),
    )
    stored = db.whole_tasks.find_one({"_id": created["id"]})
    assert len(stored["brand"]["reference_asset_ids"]) == 5
    assert len(stored["brand"]["references"]) == 5
    restored = brand_style.brand_for_task(stored, db, store)
    assert len(restored.reference_inputs) == 5
    for path in restored.reference_paths:
        path.unlink(missing_ok=True)

    with pytest.raises(HTTPException) as exc:
        brand_routes.upload_reference("seejoy", request, _upload_png())
    assert exc.value.status_code == 422
    assert "最多 5 张" in str(exc.value.detail)


def test_task_brand_snapshot_keeps_guides_and_loads_style_references(
    tmp_path, settings, store, product_png
):
    db = mongomock.MongoClient().db
    _write_skill(tmp_path, "custom", "custom", is_default=True)
    _import(db, settings, store, tmp_path)
    product_id = _seed_product(settings, store, db, product_png)

    created = whole_routes.create_task(
        WholeImageTaskCreate(prompt="test", product_asset_ids=[product_id], brand_id="custom"),
        _task_request(db, settings, store),
        BackgroundTasks(),
    )
    stored = db.whole_tasks.find_one({"_id": created["id"]})
    assert stored["brand"]["layout_guide"] == "LAYOUT GUIDE 测试规范"
    assert stored["brand"]["background_guide"] == "BACKGROUND GUIDE 测试背景"
    assert stored["brand"]["reference_asset_ids"]
    assert "skill_dir" not in stored["brand"]

    db.brands.update_one({"_id": "custom"}, {"$set": {"layout_guide": "CHANGED"}})
    restored = brand_style.brand_for_task(stored, db, store)
    assert restored.layout_guide == "LAYOUT GUIDE 测试规范"
    assert len(restored.reference_paths) == 1
    assert restored.reference_paths[0].exists()
    for path in restored.reference_paths:
        path.unlink(missing_ok=True)


def test_task_snapshot_rejects_missing_reference_asset(
    tmp_path, settings, store, product_png
):
    db = mongomock.MongoClient().db
    _write_skill(tmp_path, "custom", "custom", is_default=True)
    _import(db, settings, store, tmp_path)
    product_id = _seed_product(settings, store, db, product_png)
    brand_doc = db.brands.find_one({"_id": "custom"})
    missing_id = brand_doc["reference_asset_ids"][0]
    db.assets.delete_one({"_id": missing_id})

    with pytest.raises(HTTPException) as exc:
        whole_routes.create_task(
            WholeImageTaskCreate(prompt="test", product_asset_ids=[product_id], brand_id="custom"),
            _task_request(db, settings, store),
            BackgroundTasks(),
        )
    assert exc.value.status_code == 422
    assert "品牌参考图" in str(exc.value.detail)


def test_old_task_without_snapshot_falls_back_to_mongo_brand(tmp_path, settings, store):
    db = mongomock.MongoClient().db
    _write_skill(tmp_path, "custom", "custom", is_default=True)
    _import(db, settings, store, tmp_path)

    restored = brand_style.brand_for_task({"input": {"brand_id": "custom"}}, db, store)
    assert restored.id == "custom"
    assert restored.layout_guide == "LAYOUT GUIDE 测试规范"


def test_old_task_with_unknown_brand_has_no_style(settings, store):
    db = mongomock.MongoClient().db
    restored = brand_style.brand_for_task({"input": {"brand_id": "missing"}}, db, store)
    assert restored is None


def test_task_without_brand_has_no_style_snapshot(settings, store, product_png):
    db = mongomock.MongoClient().db
    _import(db, settings, store, PROJECT_DIR / ".agents" / "skills")
    product_id = _seed_product(settings, store, db, product_png)

    created = whole_routes.create_task(
        WholeImageTaskCreate(prompt="test", product_asset_ids=[product_id]),
        _task_request(db, settings, store),
        BackgroundTasks(),
    )
    assert created["input"]["brand_id"] is None
    assert created["input"]["brand_mode"] == "none"
    assert created["brand"]["id"] is None
    assert created["brand"]["name"] is None
    stored = db.whole_tasks.find_one({"_id": created["id"]})
    assert stored["brand"] is None


def test_task_accepts_explicit_brand_and_rejects_unknown(settings, store, product_png):
    db = mongomock.MongoClient().db
    _import(db, settings, store, PROJECT_DIR / ".agents" / "skills")
    product_id = _seed_product(settings, store, db, product_png)
    request = _task_request(db, settings, store)

    created = whole_routes.create_task(
        WholeImageTaskCreate(prompt="test", product_asset_ids=[product_id], brand_id="seejoy"),
        request,
        BackgroundTasks(),
    )
    assert created["brand"]["id"] == "seejoy"
    with pytest.raises(HTTPException) as exc:
        whole_routes.create_task(
            WholeImageTaskCreate(prompt="test", product_asset_ids=[product_id], brand_id="missing"),
            request,
            BackgroundTasks(),
        )
    assert exc.value.status_code == 422


def test_task_brand_snapshot_registers_immutable_references(
    settings, store, product_png
):
    db = mongomock.MongoClient().db
    _import(db, settings, store, PROJECT_DIR / ".agents" / "skills")
    product_id = _seed_product(settings, store, db, product_png)
    created = whole_routes.create_task(
        WholeImageTaskCreate(prompt="test", product_asset_ids=[product_id], brand_mode="preset", brand_id="seejoy"),
        _task_request(db, settings, store),
        BackgroundTasks(),
    )
    stored = db.whole_tasks.find_one({"_id": created["id"]})
    refs = stored["brand"]["references"]
    assert len(refs) == 3
    assert all(ref["asset_id"] and len(ref["sha256"]) == 64 for ref in refs)
    assert all(db.assets.find_one({"_id": ref["asset_id"]})["kind"] == "brand_reference" for ref in refs)
    restored = brand_style.brand_for_task(stored, db, store)
    assert len(restored.reference_paths) == 3
    assert all(path.exists() for path in restored.reference_paths)
    for path in restored.reference_paths:
        path.unlink(missing_ok=True)
