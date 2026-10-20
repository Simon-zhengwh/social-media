from __future__ import annotations

import pytest

from app.services import platform_presets


def test_catalog_exposes_platform_placements_and_contact_policy():
    catalog = platform_presets.catalog()
    by_id = {item["id"]: item for item in catalog}

    assert [item["id"] for item in catalog] == [
        "xiaohongshu",
        "instagram",
        "tiktok",
        "google-business-profile",
        "meta-ads",
    ]
    assert by_id["xiaohongshu"]["contact_policy"] == "forbidden"
    assert by_id["xiaohongshu"]["default_placement_id"] == "note"
    assert by_id["xiaohongshu"]["placements"][0]["width"] == 1080
    assert by_id["xiaohongshu"]["placements"][0]["height"] == 1440
    assert by_id["google-business-profile"]["placements"][0]["ratio"] == "4:3"
    instagram = {item["id"]: item for item in by_id["instagram"]["placements"]}
    assert instagram["story"]["ratio"] == "9:16"
    assert instagram["story"]["safe_area"]["top"] == 0.15
    assert by_id["meta-ads"]["contact_policy"] == "required"


def test_resolve_placement_uses_default_and_rejects_unknown_pair():
    platform, placement = platform_presets.resolve_placement("instagram", None)
    assert platform.id == "instagram"
    assert placement.id == "portrait"

    with pytest.raises(platform_presets.PlatformError):
        platform_presets.resolve_placement("instagram", "note")


def test_platform_builds_creation_snapshot_and_validates_merchant_info():
    snapshot = platform_presets.build_task_config(
        "meta-ads",
        "feed",
        {"address": "上海市徐汇区某路 88 号", "phone": "021-1234 5678"},
    )
    assert snapshot["ratio"] == "4:5"
    assert snapshot["canvas"] == {"width": 1080, "height": 1350}
    assert snapshot["merchant_info"]["phone"] == "021-1234 5678"

    with pytest.raises(platform_presets.PlatformError, match="商家地址"):
        platform_presets.build_task_config("instagram", "portrait", {"phone": "123"})


def test_prompt_context_locks_size_and_contact_rules():
    platform = platform_presets.get_platform("xiaohongshu")
    placement = platform_presets.resolve_placement("xiaohongshu", "note")[1]
    xhs_context = platform_presets.prompt_context(platform, placement, None)
    assert "1080×1440" in xhs_context
    assert "不得出现地址" in xhs_context

    meta = platform_presets.get_platform("meta-ads")
    meta_placement = platform_presets.resolve_placement("meta-ads", "story")[1]
    meta_context = platform_presets.prompt_context(
        meta,
        meta_placement,
        {"address": "门店地址 A", "phone": "400-000-0000"},
    )
    assert "门店地址 A" in meta_context
    assert "400-000-0000" in meta_context


def test_task_create_accepts_platform_and_rejects_missing_contact(settings, store):
    from types import SimpleNamespace
    from fastapi import HTTPException, BackgroundTasks
    import mongomock
    from app.routes import tasks as task_routes
    from app.schemas import TaskCreate

    db = mongomock.MongoClient().db
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    db.brands.insert_one(
        {
            "_id": "seejoy",
            "name": "SEEJOY 喜聚",
            "short_name": "喜聚",
            "summary": "test",
            "is_default": True,
            "layout_guide": "TEST LAYOUT",
            "background_guide": "TEST BACKGROUND",
            "logo_keywords": [],
            "cover_asset_id": None,
            "reference_asset_ids": [],
            "created_at": now,
            "updated_at": now,
        }
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(db=db, settings=settings, store=store, core_ai=None)
        )
    )

    created = task_routes.create_task(
        TaskCreate(prompt="新品海报", platform_id="xiaohongshu", placement_id="note"),
        request,
        BackgroundTasks(),
    )
    assert created["canvas"] == {"width": 1080, "height": 1440}
    assert created["input"]["ratio"] == "3:4"
    assert created["platform"]["id"] == "xiaohongshu"
    assert created["input"].get("merchant_info") is None

    contact_request = task_routes.create_task(
        TaskCreate(
            prompt="海报底部写上门店地址和电话",
            platform_id="xiaohongshu",
            placement_id="note",
        ),
        request,
        BackgroundTasks(),
    )
    assert contact_request["status"] == "queued"

    with pytest.raises(HTTPException) as error:
        task_routes.create_task(
            TaskCreate(prompt="新品海报", platform_id="instagram", placement_id="portrait"),
            request,
            BackgroundTasks(),
        )
    assert error.value.status_code == 422
    assert "地址和联系电话" in str(error.value.detail)


def test_task_can_use_free_brand_with_optional_style_references(settings, store):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from fastapi import BackgroundTasks
    import mongomock
    from app.routes import tasks as task_routes
    from app.schemas import TaskCreate

    db = mongomock.MongoClient().db
    now = datetime.now(timezone.utc)
    db.assets.insert_many(
        [
            {"_id": "asset_product", "kind": "product", "created_at": now},
            {"_id": "asset_style", "kind": "brand_reference", "created_at": now},
        ]
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(db=db, settings=settings, store=store, core_ai=None)
        )
    )
    created = task_routes.create_task(
        TaskCreate(
            product_asset_ids=["asset_product"],
            brand_mode="reference",
            style_reference_asset_ids=["asset_style"],
            prompt="自由发挥一张新品海报",
            platform_id="xiaohongshu",
            placement_id="note",
        ),
        request,
        BackgroundTasks(),
    )
    assert created["brand"] is None
    assert created["input"]["brand_id"] is None
    assert created["input"]["style_reference_asset_ids"] == ["asset_style"]


def test_candidate_batch_returns_candidates_in_index_order(settings, store):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from fastapi import BackgroundTasks
    import mongomock
    from app.routes import tasks as task_routes
    from app.schemas import TaskCreate

    db = mongomock.MongoClient().db
    db.assets.insert_one(
        {"_id": "asset_product", "kind": "product", "created_at": datetime.now(timezone.utc)}
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(db=db, settings=settings, store=store, core_ai=None)
        )
    )
    batch_id = "batch_test"
    for index in (1, 0):
        task_routes.create_task(
            TaskCreate(
                product_asset_ids=["asset_product"],
                prompt=f"候选 {index}",
                candidate_count=3,
                candidate_batch_id=batch_id,
                candidate_index=index,
            ),
            request,
            BackgroundTasks(),
        )
    batch = task_routes.get_task_batch(batch_id, request)
    assert [item["input"]["candidate_index"] for item in batch] == [0, 1]
    assert all(item["input"]["candidate_batch_id"] == batch_id for item in batch)
