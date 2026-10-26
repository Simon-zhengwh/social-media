from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import mongomock

from app.config import Settings
from app.services import storage
from app.services.object_store import LocalObjectStore
from scripts.backfill_local_asset_cache import backfill


def test_backfill_matches_asset_by_hash_without_changing_database(tmp_path, product_png):
    source_dir = tmp_path / "samples"
    source_dir.mkdir()
    source = source_dir / "dish.png"
    source.write_bytes(product_png)
    settings = Settings(storage_dir=str(tmp_path / "storage"))
    db = mongomock.MongoClient().db
    document = {
        "_id": "asset_test",
        "kind": "product",
        "original_name": "dish.png",
        "path": "social-media/originals/asset_test.png",
        "prepared_path": "social-media/prepared/asset_test.png",
        "mime_type": "image/png",
        "sha256": hashlib.sha256(product_png).hexdigest(),
        "created_at": datetime.now(timezone.utc),
    }
    db.assets.insert_one(document)
    stored = db.assets.find_one({'_id': 'asset_test'})

    result = backfill(db, settings, source_roots=[source_dir], apply=True)
    cache = LocalObjectStore(settings.storage_path / "objects")

    assert result["restored"] == ["asset_test"]
    assert cache.get_bytes(document["path"]) == product_png
    with storage.open_image(cache, document["prepared_path"]) as image:
        assert image.size == (320, 240)
    assert db.assets.find_one({"_id": "asset_test"}) == stored
