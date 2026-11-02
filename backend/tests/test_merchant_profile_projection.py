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
