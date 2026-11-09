from app.services import merchant_profile
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


def test_brand_info_keeps_year_brand_wide_and_address_per_store():
 from app.services.merchant_profile import project_profile

 profile = {
 "merchant_id": "merchant-keke",
 "brand": {"canonical_name": "Keke", "year_established":2019},
 "locations": [
 {"place_id": "p1", "name": "Flushing",
 "nap": {"address": "34-20 Linden Pl", "suite": "1st Fl", "phone": "(347)438-1030"}},
 {"place_id": "p2", "name": "Murray Hill",
 "nap": {"address": "150-15 Northern Blvd", "suite": "", "phone": "(718)555-0100"}},
 ],
 }
 info = project_profile(profile)["brand_info"]

 assert info["year_established"] == 2019
 assert [store["name"] for store in info["stores"]] == ["Flushing", "Murray Hill"]
 assert [store["address"] for store in info["stores"]] == ["34-20 Linden Pl", "150-15 Northern Blvd"]
 assert info["stores"][0]["suite"] == "1st Fl"
 assert info["stores"][1]["phone"] == "(718)555-0100"


def test_brand_info_survives_missing_year_and_missing_nap():
 from app.services.merchant_profile import project_profile

 profile = {
 "merchant_id": "m",
 "brand": {"canonical_name": "M"},
 "locations": [{"place_id": "p1", "name": "Store"}, {"name": "No place id"}],
 }
 info = project_profile(profile)["brand_info"]

 assert info["year_established"] is None
 assert info["stores"][0] == {"place_id": "p1", "name": "Store", "address": "", "suite": "", "phone": ""}
 assert info["stores"][1]["name"] == "No place id"


def test_brand_info_does_not_leak_into_the_model_background():
 from app.services.merchant_profile import project_profile

 profile = {
 "merchant_id": "m",
 "brand": {"canonical_name": "M", "year_established":1999, "positioning": "P"},
 "locations": [{"place_id": "p1", "name": "S",
 "nap": {"address": "SECRET_ADDRESS", "phone": "SECRET_PHONE"}}],
 }
 projected = project_profile(profile)
 text = " ".join(section["text"] for section in projected["prompt_sections"])

 assert "SECRET_ADDRESS" not in text
 assert "SECRET_PHONE" not in text
 assert "1999" not in text


profile = {
	"merchant_id": "m",
	"name": "Keke",
	"prompt_sections": [{"title": "商户定位", "text": "卤味"}],
	"locations": [
		{"place_id": "p1", "name": "Flushing",
		 "nap": {"address": "34-20 Linden Pl", "suite": "1st Fl", "phone": "(347)438-1030"}},
		{"place_id": "p2", "name": "Murray Hill",
		 "nap": {"address": "150-15 Northern Blvd", "suite": "", "phone": "(718)555-0100"}},
	],
}


def test_nothing_selected_adds_no_store_block():
	assert merchant_profile.store_facts(profile, "p1", include_address=False, include_phone=False) is None
	prompt = merchant_profile.prompt_context(profile, None)
	assert "34-20 Linden Pl" not in prompt
	assert "(347)438-1030" not in prompt


def test_address_only_omits_the_phone():
	facts = merchant_profile.store_facts(profile, "p1", include_address=True, include_phone=False)

	assert facts is not None
	assert facts["address"] == "34-20 Linden Pl 1st Fl"
	assert "phone" not in facts
	block = merchant_profile.store_context(facts)
	assert "34-20 Linden Pl" in block
	assert "438-1030" not in block


def test_phone_only_omits_the_address():
	facts = merchant_profile.store_facts(profile, "p1", include_address=False, include_phone=True)

	assert facts is not None
	assert facts["phone"] == "(347)438-1030"
	assert "address" not in facts
	block = merchant_profile.store_context(facts)
	assert "438-1030" in block
	assert "Linden" not in block


def test_both_fields_are_joined_into_one_block():
	facts = merchant_profile.store_facts(profile, "p1", include_address=True, include_phone=True)
	block = merchant_profile.store_context(facts)

	assert "34-20 Linden Pl" in block
	assert "(347)438-1030" in block
	assert "逐字照抄" in block


def test_only_the_selected_store_is_used():
	facts = merchant_profile.store_facts(profile, "p2", include_address=True, include_phone=True)

	assert facts["address"] == "150-15 Northern Blvd"
	assert facts["phone"] == "(718)555-0100"


def test_unknown_store_contributes_nothing():
	assert merchant_profile.store_facts(profile, "nope", include_address=True, include_phone=True) is None
	assert merchant_profile.store_facts(profile, "", include_address=True, include_phone=True) is None


def test_store_block_joins_the_merchant_background():
	prompt = merchant_profile.prompt_context(profile, {"name": "Flushing", "address": "34-20 Linden Pl", "phone": "(347)438-1030"})

	assert "商户背景参考" in prompt
	assert "门店信息" in prompt
	assert prompt.index("商户背景参考") < prompt.index("门店信息")


def test_prompt_without_profile_can_still_carry_store_facts():
	prompt = merchant_profile.prompt_context(None, {"address": "34-20 Linden Pl"})

	assert "34-20 Linden Pl" in prompt
	assert "商户背景参考" not in prompt
