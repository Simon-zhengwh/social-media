from __future__ import annotations

from datetime import datetime, timezone

import mongomock
import pytest

from app.services import maintenance
from app.services.scoped_db import ScopedDatabase


def test_initialize_is_idempotent_and_preserves_maintenance_state():
    db = mongomock.MongoClient().db
    maintenance.initialize(db)
    maintenance.enable(db)
    maintenance.initialize(db)
    state = maintenance.status(db)
    assert state["enabled"] is True
    assert state["admitting"] == 0


def test_admission_is_atomic_with_enable_and_status_counts_every_workflow():
    db = mongomock.MongoClient().db
    maintenance.initialize(db)
    with maintenance.admit(db):
        assert maintenance.status(db)["admitting"] == 1
        maintenance.enable(db)
        with pytest.raises(maintenance.MaintenanceError):
            with maintenance.admit(db):
                pass
    assert maintenance.status(db)["admitting"] == 0
    db.whole_tasks.insert_one({"status": "running"})
    db.whole_tasks.insert_one({"status": "succeeded", "edit": {"status": "running"}})
    db.poster_edit_tasks.insert_one({"status": "queued"})
    db.background_replace_tasks.insert_one({"status": "running"})
    state = maintenance.status(db)
    assert state["active_tasks"] == {"whole_tasks": 1, "whole_edits": 1, "poster_edits": 1, "background_replacements": 1, "agent_tests": 0}
    assert state["drained"] is False


def test_scoped_database_isolates_queries_updates_upserts_and_brand_rules():
    raw = mongomock.MongoClient().db
    a = ScopedDatabase(raw, "merchant-a")
    b = ScopedDatabase(raw, "merchant-b")
    a.assets.insert_one({"_id": "asset-a", "sha256": "same", "created_at": datetime.now(timezone.utc)})
    b.assets.insert_one({"_id": "asset-b", "sha256": "same", "created_at": datetime.now(timezone.utc)})
    assert a.assets.find_one({"_id": "asset-b"}) is None
    assert b.assets.find_one({"_id": "asset-a"}) is None
    assert a.assets.find_one({"_id": "asset-a"})["merchant_id"] == "merchant-a"
    assert b.assets.find_one({"_id": "asset-b"})["merchant_id"] == "merchant-b"

    a.brand_rules.update_one(
        {"_id": "merchant-a", "version": 1},
        {"$set": {"version": 2, "merchant_id": "merchant-a", "readability": "A"}},
        upsert=True,
    )
    b.brand_rules.update_one(
        {"_id": "merchant-b", "version": 1},
        {"$set": {"version": 2, "merchant_id": "merchant-b", "readability": "B"}},
        upsert=True,
    )
    assert a.brand_rules.find_one({"_id": "merchant-a"})["readability"] == "A"
    assert b.brand_rules.find_one({"_id": "merchant-b"})["readability"] == "B"
    assert a.brand_rules.find_one({"_id": "merchant-b"}) is None
