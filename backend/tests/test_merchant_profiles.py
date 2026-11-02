from types import SimpleNamespace

import mongomock

from app.routes.brands import _brand_detail, duplicate_brand, update_brand
from app.schemas import BrandUpdate, DuplicateBrandRequest
from app.services.merchant_profile import project_profile
from app.services.scoped_db import ScopedDatabase


def request(db, merchant_id="merchant", profile=None):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db=db, store=None)),
        state=SimpleNamespace(
            merchant_id=merchant_id,
            merchant_profile=profile,
            db=ScopedDatabase(db, merchant_id),
            store=None,
        ),
        path_params={"merchant_id": merchant_id},
    )


def seed(db, brand_id):
    doc = {"_id": brand_id, "name": brand_id, "platform_id": "xiaohongshu", "placement_id": "note", "layout_guide": "existing", "background_guide": "existing"}
    db.brands.insert_one(doc)
    return db.brands.find_one({"_id": brand_id})


def test_am_profile_is_the_only_merchant_profile_source_and_ignores_local_legacy_collection():
    db = ScopedDatabase(mongomock.MongoClient().db, "merchant")
    doc = seed(db, "seejoy")
    db.merchant_profiles.insert_one({"_id": "merchant", "name": "LOCAL SEED MUST NOT BE USED"})
    req = request(db.raw, profile=None)
    assert _brand_detail(req, db, doc)["merchant_profile"] is None


def test_projected_am_profile_is_shared_by_styles_and_keeps_source_paths():
    db = ScopedDatabase(mongomock.MongoClient().db, "merchant")
    profile = project_profile({
        "merchant_id": "merchant",
        "brand": {"canonical_name": "GEORGE", "positioning": "gathering", "personality": "warm", "voice": {"description": "friendly"}},
        "locations": [{"place_id": "place-1", "name": "One", "core_highlights": ["hot pot"]}],
    })
    req = request(db.raw, profile=profile)
    for brand_id in ("xhs-style", "ig-style"):
        result = _brand_detail(req, db, seed(db, brand_id))["merchant_profile"]
        assert result["source_record_id"] == "merchant"
        assert result["sections"][1]["source_path"] == "brand.positioning"
        assert result["location_place_ids"] == ["place-1"]


def test_style_save_and_duplicate_preserve_merchant_scope():
    raw = mongomock.MongoClient().db
    profile = {"id": "merchant", "name": "GEORGE", "source_label": "AM Console", "source_record_id": "merchant", "sections": []}
    req = request(raw, profile=profile)
    seed(req.state.db, "linked")
    updated = update_brand("linked", BrandUpdate(name="Changed style", layout_guide="layout", background_guide="background"), req)
    assert updated["merchant_profile"]["id"] == "merchant"
    copied = duplicate_brand("linked", DuplicateBrandRequest(platform_id="instagram", placement_id="story"), req)
    assert copied["merchant_profile"]["id"] == "merchant"
    assert copied["layout_guide"] == "layout"
    assert req.state.db.brands.find_one({"_id": "linked"})["merchant_id"] == "merchant"
    assert req.state.db.brands.find_one({"_id": copied["id"]})["merchant_id"] == "merchant"
