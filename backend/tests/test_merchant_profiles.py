import json
from pathlib import Path
from types import SimpleNamespace

import mongomock

from app.routes.brands import _brand_detail, duplicate_brand, update_brand
from app.schemas import BrandUpdate, DuplicateBrandRequest


def seed(db, brand_id, merchant_id=None):
    doc = {"_id": brand_id, "name": brand_id, "platform_id": "xiaohongshu", "placement_id": "note", "layout_guide": "existing", "background_guide": "existing"}
    if merchant_id:
        doc["merchant_id"] = merchant_id
    db.brands.insert_one(doc)
    return doc


def request(db):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db)))


def test_unlinked_brand_does_not_guess_merchant_from_name():
    db = mongomock.MongoClient().db
    doc = seed(db, "seejoy")
    db.merchant_profiles.insert_one({"_id": "merchant", "name": "SEE JOY"})
    assert _brand_detail(db, doc)["merchant_profile"] is None


def test_shared_profile_only_returns_model_background_and_preserves_original():
    db = mongomock.MongoClient().db
    profile = json.loads((Path(__file__).parents[1] / "data/merchant_profiles/seejoy.json").read_text(encoding="utf-8"))
    profile["_id"] = "merchant"
    db.merchant_profiles.insert_one(profile)
    for brand_id in ("xhs-style", "ig-style"):
        result = _brand_detail(db, seed(db, brand_id, "merchant"))["merchant_profile"]
        assert result["id"] == "merchant"
        assert [section["title"] for section in result["prompt_sections"]] == ["商户定位", "核心卖点", "目标客群", "表达语气"]
        assert [section["text"] for section in result["prompt_sections"]] == [profile["sections"][index]["text"] for index in (0, 1, 2, 4)]
        assert all(set(section) == {"title", "text"} for section in result["prompt_sections"])
        assert result["sections"] == profile["sections"]
        assert result["source_record_id"] == profile["source_record_id"]
        assert "source" not in result
    assert db.merchant_profiles.find_one({"_id": "merchant"}) == profile


def test_empty_or_missing_merchant_background_is_not_displayed():
    db = mongomock.MongoClient().db
    doc = seed(db, "linked", "merchant")
    assert _brand_detail(db, doc)["merchant_profile"] is None
    db.merchant_profiles.insert_one({"_id": "merchant", "sections": [
        {"source_path": "brand.positioning", "text": "  "},
        {"source_path": "locations.0.nap.address", "text": "ADDRESS_NOT_FOR_MODEL"},
    ]})
    assert _brand_detail(db, doc)["merchant_profile"]["prompt_sections"] == []


def test_style_save_and_duplicate_preserve_merchant_link():
    db = mongomock.MongoClient().db
    seed(db, "linked", "merchant")
    db.merchant_profiles.insert_one({"_id": "merchant", "name": "SEE JOY", "sections": [
        {"source_path": "brand.positioning", "text": "单人火锅"},
    ]})
    updated = update_brand("linked", BrandUpdate(name="Changed style", layout_guide="layout", background_guide="background"), request(db))
    assert updated["merchant_profile"]["id"] == "merchant"
    copied = duplicate_brand("linked", DuplicateBrandRequest(platform_id="instagram", placement_id="story"), request(db))
    assert copied["merchant_profile"]["id"] == "merchant"
    assert copied["layout_guide"] == "layout"


def test_seed_profile_preserves_source_and_rejects_mismatched_original():
    import json
    from pathlib import Path

    import pytest

    from scripts.import_merchant_profile import validate_profile

    profile = json.loads((Path(__file__).parents[1] / "data/merchant_profiles/seejoy.json").read_text(encoding="utf-8"))
    validate_profile(profile)
    assert profile["source_record_id"] == "f0750173-0113-464d-bb90-cbebb9c08ba7"
    assert profile["source"]["brand"]["canonical_name"] == "SEE JOY 喜聚"
    profile["sections"][0]["original"] = "fabricated"
    with pytest.raises(ValueError, match="Source mismatch"):
        validate_profile(profile)
