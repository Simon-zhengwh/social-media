from copy import deepcopy
from datetime import datetime, timezone

import mongomock
import pytest
from bson import json_util

from scripts import initialize_brand_templates as migration


def seeded_database():
    db = mongomock.MongoClient().db
    db.brands.insert_many([
        {"_id": brand_id, "summary": "原有定位", "layout_guide": "原始主规范", "background_guide": "原始背景规范", "colors": [{"hex": "#123456"}] if brand_id == "seejoy" else [], "logo_asset_ids": ["logo"], "reference_asset_ids": ["ref"], "cover_asset_id": "ref", "updated_at": datetime(2026, 10, 20, tzinfo=timezone.utc)}
        for brand_id in ("seejoy", "duoyi")
    ])
    db.assets.insert_one({"_id": "ref"})
    db.whole_tasks.insert_one({"_id": "task", "snapshot": "historical"})
    return db


def test_preview_never_writes_database_or_backup(tmp_path):
    db = seeded_database()
    before = list(db.brands.find())
    report = migration.migrate(db, tmp_path)
    assert report["mode"] == "preview"
    assert len(report["changes"]) == 2
    assert report["initialize_common_rules"]
    assert list(db.brands.find()) == before
    assert db.brand_rules.count_documents({}) == 0
    assert list(tmp_path.iterdir()) == []


def test_apply_backups_and_preserves_data_and_rerun_edits(tmp_path):
    db = seeded_database()
    original = list(db.brands.find())
    original_task = db.whole_tasks.find_one()
    report = migration.migrate(db, tmp_path, apply=True)
    assert report["migrated"] == ["seejoy", "duoyi"]
    assert report["preserved"]
    with open(report["backup_path"], encoding="utf-8") as stream:
        backup = json_util.loads(stream.read())
    assert backup["brands"] == original
    assert backup["brand_rules"] == []
    assert db.whole_tasks.find_one() == original_task
    for doc in original:
        changed = db.brands.find_one({"_id": doc["_id"]})
        for key in migration.PRESERVED:
            assert changed.get(key) == doc.get(key)
        assert changed["legacy_guides"]["layout_guide"] == doc["layout_guide"]
        assert changed["schema_version"] == 2
    duoyi = db.brands.find_one({"_id": "duoyi"})
    assert duoyi["platform_id"] == "instagram" and duoyi["placement_id"] == "story"
    assert duoyi["purpose"] == "Story / Reels 封面"
    assert "五指" in duoyi["style_fields"]["restrictions"]
    assert "尖端指向菜名" in duoyi["style_fields"]["decoration"]
    assert "蒙版" not in str(duoyi["style_fields"])
    db.brands.update_one({"_id": "duoyi"}, {"$set": {"style_fields.identity": "用户后来修改"}})
    db.brand_rules.update_one({"_id": "default"}, {"$set": {"readability": "用户通用规范", "version": 2}})
    expected = deepcopy(list(db.brands.find()))
    second = migration.migrate(db, tmp_path, apply=True)
    assert second["migrated"] == []
    assert list(db.brands.find()) == expected
    assert db.brand_rules.find_one()["readability"] == "用户通用规范"


def test_backup_failure_stops_all_writes(tmp_path, monkeypatch):
    db = seeded_database()
    expected = list(db.brands.find())

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(migration, "write_backup", fail)
    with pytest.raises(OSError, match="disk full"):
        migration.migrate(db, tmp_path, apply=True)
    assert list(db.brands.find()) == expected
    assert db.brand_rules.count_documents({}) == 0


@pytest.mark.parametrize("key,value", [("updated_at", datetime(2026, 10, 26)), ("layout_guide", "new guide"), ("schema_version", 2)])
def test_concurrent_edits_are_not_overwritten(tmp_path, monkeypatch, key, value):
    db = seeded_database()
    write_backup = migration.write_backup

    def change_after_backup(path, payload):
        write_backup(path, payload)
        db.brands.update_one({"_id": "duoyi"}, {"$set": {key: value}})

    monkeypatch.setattr(migration, "write_backup", change_after_backup)
    report = migration.migrate(db, tmp_path, apply=True)
    assert report["conflicts"] == ["duoyi"]
    assert report["migrated"] == ["seejoy"]
    assert db.brands.find_one({"_id": "duoyi"})[key] == value
    assert "legacy_guides" not in db.brands.find_one({"_id": "duoyi"})
