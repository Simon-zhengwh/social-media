from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import mongomock

from app.services.object_store import LocalObjectStore
from scripts.migrate_media_to_local import migrate_media_to_local


class FakeSourceStore:
    def __init__(self, objects):
        self.objects = objects

    def get_bytes(self, key):
        return self.objects[key]


def test_migrate_media_to_local_downloads_materials_brands_and_latest_tasks(tmp_path):
    db = mongomock.MongoClient().db
    asset = {
        "_id": "asset_product",
        "kind": "product",
        "original_name": "菜品.jpg",
        "path": "social-media/originals/asset_product.jpg",
        "prepared_path": "social-media/prepared/asset_product.png",
        "mime_type": "image/jpeg",
        "created_at": datetime(2026, 10, 26, tzinfo=timezone.utc),
    }
    output = {
        "_id": "asset_output",
        "kind": "output",
        "original_name": "task-v1.png",
        "path": "social-media/outputs/task-v1.png",
        "prepared_path": "social-media/outputs/task-v1.png",
        "mime_type": "image/png",
        "created_at": datetime(2026, 10, 26, tzinfo=timezone.utc),
    }
    db.assets.insert_many([asset, output])
    db.brands.insert_one(
        {
            "_id": "seejoy",
            "name": "喜聚",
            "cover_asset_id": "asset_product",
            "logo_asset_ids": [],
            "reference_asset_ids": ["asset_product"],
        }
    )
    db.whole_tasks.insert_one(
        {
            "_id": "wtask_latest",
            "created_at": datetime(2026, 10, 26, tzinfo=timezone.utc),
            "input": {"product_asset_ids": ["asset_product"], "logo_asset_id": None},
            "brand": {"logo_asset_ids": [], "reference_asset_ids": []},
            "output_asset_id": "asset_output",
        }
    )
    db.whole_versions.insert_one(
        {"_id": "wver_1", "task_id": "wtask_latest", "version": 1, "output_asset_id": "asset_output"}
    )
    source = FakeSourceStore({
        asset["path"]: b"original",
        asset["prepared_path"]: b"prepared",
        output["path"]: b"generated",
    })
    local_root = tmp_path / "local_media"

    result = migrate_media_to_local(
        db,
        source,
        local_root=local_root,
        recent_task_limit=10,
        apply=True,
    )

    runtime_store = LocalObjectStore(local_root / "objects")
    assert runtime_store.get_bytes(asset["prepared_path"]) == b"prepared"
    assert runtime_store.get_bytes(output["path"]) == b"generated"
    assert (local_root / "archive" / "materials").rglob("*菜品.jpg").__next__()
    assert (local_root / "archive" / "brand-styles" / "seejoy").rglob("*").__next__()
    assert (local_root / "archive" / "recent-tasks" / "wtask_latest").rglob("*").__next__()
    assert result["tasks"] == ["wtask_latest"]
    assert result["missing_assets"] == []
    assert result["missing_objects"] == []
