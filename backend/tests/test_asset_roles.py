from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import mongomock

from app.routes import assets as asset_routes
from app.routes import brands as brand_routes
from app.routes import whole_image as whole_routes
from app.schemas import WholeImageTaskCreate
from app.services import storage


def _asset_document(asset_id: str, *, source: str | None = None, legacy_kind: str | None = None):
    now = datetime.now(timezone.utc)
    document = {
        "_id": asset_id,
        "original_name": f"{asset_id}.png",
        "path": f"originals/{asset_id}.png",
        "prepared_path": f"prepared/{asset_id}.png",
        "mime_type": "image/png",
        "width": 320,
        "height": 240,
        "bytes": 10,
        "sha256": asset_id,
        "created_at": now,
    }
    if source is not None:
        document["source"] = source
    if legacy_kind is not None:
        document["kind"] = legacy_kind
    return document


class _ConfiguredClient:
    configured = True


def _request(db, settings, store):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                db=db,
                settings=settings,
                store=store,
                core_ai=_ConfiguredClient(),
            )
        )
    )


def test_prepare_upload_has_no_intrinsic_asset_role(settings, store, product_png):
    result = storage.prepare_upload(settings, store, product_png, "dish.png")

    assert not hasattr(result, "kind")


def test_asset_library_lists_all_uploaded_images_and_excludes_generated_images(settings, store):
    db = mongomock.MongoClient().db
    db.assets.insert_many(
        [
            _asset_document("upload", source="upload"),
            _asset_document("legacy-upload", legacy_kind="logo"),
            _asset_document("generated", source="generated"),
            _asset_document("legacy-output", legacy_kind="output"),
        ]
    )

    items = asset_routes.list_assets(_request(db, settings, store))

    assert {item["id"] for item in items} == {"upload", "legacy-upload"}
    assert all("kind" not in item for item in items)


def test_same_image_can_be_used_as_content_logo_and_style_reference(settings, store):
    db = mongomock.MongoClient().db
    db.assets.insert_one(_asset_document("shared-image", source="upload"))
    request = _request(db, settings, store)

    created = whole_routes.create_task(
        WholeImageTaskCreate(
            prompt="一张图同时承担不同生成角色",
            ratio="1:1",
            brand_mode="reference",
            product_asset_ids=["shared-image"],
            logo_asset_id="shared-image",
            style_reference_asset_ids=["shared-image"],
        ),
        request,
        __import__("fastapi").BackgroundTasks(),
    )

    assert created["input"]["product_asset_ids"] == ["shared-image"]
    assert created["input"]["logo_asset_id"] == "shared-image"
    assert created["input"]["style_reference_asset_ids"] == ["shared-image"]


def test_brand_logo_does_not_require_a_special_asset_kind(settings, store):
    db = mongomock.MongoClient().db
    db.assets.insert_one(_asset_document("brand-logo", source="upload"))
    now = datetime.now(timezone.utc)
    db.brands.insert_one(
        {
            "_id": "seejoy",
            "name": "SEEJOY 喜聚",
            "short_name": "喜聚",
            "summary": "test brand",
            "is_default": True,
            "layout_guide": "layout",
            "background_guide": "background",
            "logo_keywords": ["seejoy"],
            "cover_asset_id": None,
            "logo_asset_ids": ["brand-logo"],
            "reference_asset_ids": [],
            "created_at": now,
            "updated_at": now,
        }
    )

    items = brand_routes.list_brands(_request(db, settings, store))

    assert items[0]["logo_images"][0]["asset_id"] == "brand-logo"
