from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import mongomock
import pytest
from fastapi import BackgroundTasks, HTTPException
from PIL import Image

from app.config import PROJECT_DIR
from app.routes import brands as brand_routes
from app.routes import tasks as task_routes
from app.schemas import TaskCreate
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
        "version": 1,
        "is_default": is_default,
        "cover": cover,
        "logo_keywords": ["test", "测试"],
        "palette": {"primary": "#123456", "accent": "#AA1122"},
        "fonts": {"display": "brush", "body": "sans", "price": "sans"},
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


@pytest.fixture(autouse=True)
def clear_cache():
    brand_style.clear_brand_cache()
    yield
    brand_style.clear_brand_cache()


def test_project_exposes_seejoy_skill_with_three_references():
    brand = brand_style.get_brand(PROJECT_DIR / ".agents" / "skills", "seejoy")
    assert brand.name == "SEEJOY 喜聚"
    assert brand.is_default is True
    assert len(brand.reference_paths) == 3
    assert "brush" in brand.layout_guide
    assert "不生成任何文字" in brand.background_guide
    assert brand.palette["primary"] == "#0D4317"


def test_discovery_orders_default_first(tmp_path):
    _write_skill(tmp_path, "alpha", "alpha")
    _write_skill(tmp_path, "zeta", "zeta", is_default=True)
    brands = brand_style.list_brands(tmp_path)
    assert [brand.id for brand in brands] == ["zeta", "alpha"]
    assert brand_style.get_brand(tmp_path).id == "zeta"


def test_unknown_brand_raises(tmp_path):
    _write_skill(tmp_path, "alpha", "alpha", is_default=True)
    with pytest.raises(brand_style.BrandError) as exc:
        brand_style.get_brand(tmp_path, "missing")
    assert exc.value.code == "brand_not_found"


def test_invalid_json_is_skipped(tmp_path):
    _write_skill(tmp_path, "bad", "bad", raw_manifest="{not json")
    _write_skill(tmp_path, "good", "good", is_default=True)
    brands = brand_style.list_brands(tmp_path)
    assert [brand.id for brand in brands] == ["good"]


def test_duplicate_brand_id_is_skipped(tmp_path):
    first = _write_skill(tmp_path, "a-first", "same", is_default=True)
    _write_skill(tmp_path, "b-second", "same")
    brands = brand_style.list_brands(tmp_path)
    assert len(brands) == 1
    assert brands[0].layout_guide == "LAYOUT GUIDE 测试规范"
    assert brands[0].reference_paths[0].parent == (first / "assets").resolve()


def test_cover_path_traversal_is_rejected(tmp_path):
    (tmp_path / "outside.png").write_bytes(b"x")
    _write_skill(tmp_path, "bad", "bad", cover="../outside.png")
    assert brand_style.list_brands(tmp_path) == []


def test_missing_skills_directory_returns_empty(tmp_path):
    assert brand_style.list_brands(tmp_path / "missing") == []


def test_brand_routes_return_cover_and_options(settings):
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=settings))
    )
    items = brand_routes.list_brands(request)
    assert items[0]["id"] == "seejoy"
    assert items[0]["cover_url"] == "/api/brands/seejoy/cover"
    assert len(items[0]["palette"]) >= 5
    cover = brand_routes.brand_cover("seejoy", request)
    assert Path(cover.path).name == "reference-2.jpg"
    with pytest.raises(HTTPException) as exc:
        brand_routes.brand_cover("missing", request)
    assert exc.value.status_code == 404


def _task_request(db, settings, store):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(db=db, settings=settings, store=store, toapis=None)
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
        }
    )
    return prepared.id


def test_task_defaults_to_seejoy_and_snapshots_guides(settings, store, product_png):
    db = mongomock.MongoClient().db
    product_id = _seed_product(settings, store, db, product_png)
    created = task_routes.create_task(
        TaskCreate(product_asset_ids=[product_id]),
        _task_request(db, settings, store),
        BackgroundTasks(),
    )
    assert created["input"]["brand_id"] == "seejoy"
    assert created["brand"]["name"] == "SEEJOY 喜聚"
    stored = db.tasks.find_one({"_id": created["id"]})
    assert stored["brand"]["id"] == "seejoy"
    assert "brush" in stored["brand"]["layout_guide"]
    assert "不生成任何文字" in stored["brand"]["background_guide"]


def test_task_accepts_explicit_brand_and_rejects_unknown(settings, store, product_png):
    db = mongomock.MongoClient().db
    product_id = _seed_product(settings, store, db, product_png)
    request = _task_request(db, settings, store)
    created = task_routes.create_task(
        TaskCreate(product_asset_ids=[product_id], brand_id="seejoy"),
        request,
        BackgroundTasks(),
    )
    assert created["brand"]["id"] == "seejoy"
    with pytest.raises(HTTPException) as exc:
        task_routes.create_task(
            TaskCreate(product_asset_ids=[product_id], brand_id="missing"),
            request,
            BackgroundTasks(),
        )
    assert exc.value.status_code == 422
