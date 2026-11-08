from app.services.merchant_profile import project_profile


def test_project_profile_preserves_am_paths_and_multiple_locations():
    profile = {
        "merchant_id": "merchant-ume",
        "brand": {
            "legal_name": "UMETEA LLC",
            "canonical_name": "George Merchant",
            "positioning": "Gathering place",
            "personality": "Warm",
            "voice": {"description": "Friendly"},
            "artifacts": [{"secret": "not exposed"}],
        },
        "locations": [
            {"place_id": "place-1", "name": "Store One", "core_highlights": ["hot pot"], "dishes": {"signature": ["beef"]}},
            {"place_id": "place-2", "name": "Store Two", "competitive_context": {"target_customers": "groups"}},
        ],
    }
    projected = project_profile(profile)
    assert projected["source_label"].startswith("AM Console")
    assert projected["source_record_id"] == "merchant-ume"
    assert projected["location_place_ids"] == ["place-1", "place-2"]
    paths = {section["source_path"] for section in projected["sections"]}
    assert {"brand.positioning", "brand.personality", "brand.voice.description", "locations[0]", "locations[1]"} <= paths
    assert any("place-1" in section["text"] for section in projected["sections"])
    assert any("place-2" in section["text"] for section in projected["sections"])
    assert "artifacts" not in projected["raw_brand"]


def test_missing_profile_is_explicit_empty_state():
    assert project_profile(None) is None


def test_am_profile_exposes_only_four_selected_fields_to_model_background():
    from copy import deepcopy

    original = {
        "merchant_id": "merchant",
        "brand": {"canonical_name": "Merchant", "positioning": "POSITIONING",
                  "personality": "HIDDEN_PERSONALITY", "voice": {"description": "VOICE"}},
        "locations": [{"name": "Store", "core_highlights": ["HIGHLIGHT_A", "HIGHLIGHT_B"],
                       "competitive_context": {"target_customers": "CUSTOMERS", "competitors": "HIDDEN_COMPETITORS"},
                       "nap": {"address": "HIDDEN_ADDRESS"}, "dishes": ["HIDDEN_DISH"]}],
    }
    before = deepcopy(original)
    projected = project_profile(original)
    assert projected["prompt_sections"] == [
        {"title": "商户定位", "text": "POSITIONING"},
        {"title": "核心卖点", "text": "HIGHLIGHT_A；HIGHLIGHT_B"},
        {"title": "目标客群", "text": "CUSTOMERS"},
        {"title": "表达语气", "text": "VOICE"},
    ]
    from app.services.merchant_profile import prompt_context

    background = prompt_context(projected)
    assert "本次要求和备注优先" in background
    assert "平台限制" in background
    assert "HIDDEN_" not in background
    assert original == before


def test_missing_or_blank_am_background_fields_are_not_sent_to_model():
    projected = project_profile({
        "merchant_id": "merchant", "brand": {"canonical_name": "Merchant", "positioning": "  "},
        "locations": [{"core_highlights": [], "competitive_context": {"target_customers": None}}],
    })
    assert projected["prompt_sections"] == []


def test_scoped_profile_lookup_does_not_use_local_legacy_collection():
    import mongomock
    from app.services.merchant_profile import get_profile
    from app.services.scoped_db import ScopedDatabase

    db = ScopedDatabase(mongomock.MongoClient().db, "merchant")
    db.merchant_profiles.insert_one({"_id": "merchant", "name": "LEGACY", "sections": [
        {"source_path": "brand.positioning", "text": "MUST_NOT_REACH_UAT_MODEL"},
    ]})
    assert get_profile(db, "merchant") is None
