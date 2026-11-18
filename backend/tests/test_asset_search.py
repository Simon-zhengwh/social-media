from __future__ import annotations

from datetime import datetime, timedelta, timezone

import mongomock
import pytest

from app.config import Settings
from app.services import asset_index, asset_search
from app.services.asset_index import AssetIndexError
from app.services.core_ai import CoreAIError
from app.services.scoped_db import ScopedDatabase

MERCHANT = "m1"


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def chat_json(self, messages, *, model=None, timeout_seconds=None):
        self.calls += 1
        value = self.responses.pop(0) if self.responses else {}
        if isinstance(value, Exception):
            raise value
        return value


class FakeSettings:
    asset_search_model_name = "test-model"
    asset_search_timeout_seconds = 5


def vocabulary(*groups):
    return {"groups": list(groups)}


def group(input_text, *, simplified, traditional=None, english=None, expansions=()):
    return {
        "input": input_text,
        "equivalents": {
            "simplified": simplified,
            "traditional": simplified if traditional is None else traditional,
            "english": simplified if english is None else english,
        },
        "expansions": list(expansions),
    }


def seed(db, *rows):
    for asset_id, name, tags, zh, en, created in rows:
        db[asset_index.RECORD_COLLECTION].insert_one(
            {
                "_id": asset_id,
                "asset_id": asset_id,
                "name": name,
                "tags": list(tags),
                "source": "upload",
                "created_at": created,
                "url": "",
                "mime_type": "image/png",
                "description_zh": zh,
                "description_en": en,
                "keywords": {"zh": [zh] if zh else [], "en": [en] if en else []},
                "description_status": asset_index.READY,
                "description_version": 1,
            }
        )
    return db


@pytest.fixture
def db():
    return ScopedDatabase(mongomock.MongoClient().db, MERCHANT)


def test_literal_query_has_no_vocabulary_and_no_model_call():
    client = FakeClient()
    document = asset_search.resolve_query(
        ScopedDatabase(mongomock.MongoClient().db, MERCHANT),
        client=client,
        settings=FakeSettings(),
        query="牛肉 火锅",
        semantic=False,
    )
    assert client.calls == 0
    assert document["mode"] == asset_search.MODE_LITERAL
    assert document["groups"][0]["input"] == "牛肉 火锅"


def test_each_semantic_submission_calls_agent_even_for_identical_input(db):
    seed(db, ("whole", "整鸡.png", [], "整鸡", "whole chicken", datetime.now(timezone.utc)))
    client = FakeClient(
        vocabulary(group("鸡肉", simplified="鸡肉", traditional="雞肉", english="chicken")),
        vocabulary(group("鸡肉", simplified="鸡肉", traditional="雞肉", english="chicken meat")),
    )
    first = asset_search.resolve_query(db, client=client, settings=FakeSettings(), query="鸡肉", semantic=True)
    second = asset_search.resolve_query(db, client=client, settings=FakeSettings(), query="鸡肉", semantic=True)

    assert first["_id"] != second["_id"]
    assert client.calls == 2
    assert second["groups"][0]["equivalents"]["english"] == "chicken meat"


def test_repeated_search_reads_latest_assets_and_calls_agent_again(db):
    response = vocabulary(group("鸡肉", simplified="鸡肉", english="chicken"))
    client = FakeClient(response, response)
    first = asset_search.resolve_query(db, client=client, settings=FakeSettings(), query="鸡肉", semantic=True)
    assert asset_search.search_records(db, first["groups"]) == []
    seed(db, ("new", "炸鸡.png", [], "炸鸡", "fried chicken", datetime.now(timezone.utc)))

    second = asset_search.resolve_query(db, client=client, settings=FakeSettings(), query="鸡肉", semantic=True)

    assert second["_id"] != first["_id"]
    assert client.calls == 2
    assert [row["_id"] for row in asset_search.search_records(db, second["groups"])] == ["new"]


def test_semantic_vocabulary_is_not_reused_across_merchants(db):
    response = vocabulary(group("鸡肉", simplified="鸡肉", english="chicken"))
    client = FakeClient(response, response)
    first = asset_search.resolve_query(db, client=client, settings=FakeSettings(), query="鸡肉", semantic=True)
    other = ScopedDatabase(db.raw, "other-merchant")

    second = asset_search.resolve_query(other, client=client, settings=FakeSettings(), query="鸡肉", semantic=True)

    assert first["_id"] != second["_id"]
    assert client.calls == 2


def test_query_and_keyword_generation_share_standard_ingredient_names():
    from app.services import asset_recognition
    rules = asset_recognition.KEYWORD_NAMING_RULES
    assert "完整" in rules
    assert "chicken meat" not in rules
    assert "鸡肉用 chicken" not in rules
    assert rules in asset_search.QUERY_PROMPT
    assert rules in asset_recognition.KEYWORDS_PROMPT
    assert rules in asset_recognition.RECOGNITION_PROMPT


def test_paging_keeps_this_submission_without_another_agent_call(db):
    seed(db, ("whole", "整鸡.png", [], "整鸡", "whole chicken", datetime.now(timezone.utc)))
    client = FakeClient(vocabulary(group("鸡肉", simplified="鸡肉", english="chicken")))
    query = asset_search.resolve_query(db, client=client, settings=FakeSettings(), query="鸡肉", semantic=True)

    first = asset_search.search_page(db, query["_id"], skip=0, limit=1)
    second = asset_search.search_page(db, query["_id"], skip=1, limit=1)

    assert first["total"] == second["total"] == 1
    assert client.calls == 1


def test_literal_search_does_not_convert_scripts(db):
    seed(db, ("a", "擔擔麵.png", [], "", "", datetime.now(timezone.utc)))
    document = asset_index.save_query(
        db, query="担担面", mode=asset_search.MODE_LITERAL, groups=asset_search.literal_groups("担担面")
    )

    assert asset_search.search_records(db, document["groups"]) == []


@pytest.mark.parametrize("language, term", [("zh", "白盘"), ("en", "white plate")])
def test_literal_search_matches_keywords_without_translation_or_model_call(db, language, term):
    seed(db, ("a", "dish.png", [], "", "", datetime.now(timezone.utc)))
    db[asset_index.RECORD_COLLECTION].update_one(
        {"_id": "a"}, {"$set": {"keywords": {"zh": [], "en": [], language: [term]}}}
    )
    client = FakeClient()
    document = asset_search.resolve_query(db, client=client, settings=FakeSettings(), query=term, semantic=False)

    ranked = asset_search.search_records(db, document["groups"])

    assert [record["_id"] for record in ranked] == ["a"]
    assert client.calls == 0


def test_semantic_search_ands_keyword_conditions_and_ranks_equivalents_first(db):
    stamp = datetime.now(timezone.utc)
    seed(db, *((asset_id, "dish.png", [], "", "", stamp) for asset_id in ("exact", "expanded", "missing")))
    for asset_id, keywords in {
        "exact": {"zh": ["白盘"], "en": ["beef"]},
        "expanded": {"zh": ["白色盘子"], "en": ["steak"]},
        "missing": {"zh": [], "en": ["beef"]},
    }.items():
        db[asset_index.RECORD_COLLECTION].update_one({"_id": asset_id}, {"$set": {"keywords": keywords}})
    groups = asset_search.validate_vocabulary(
        vocabulary(
            group("牛肉", simplified="牛肉", english="beef", expansions=["steak"]),
            group("白盘", simplified="白盘", english="white plate", expansions=["白色盘子"]),
        ), "牛肉 白盘",
    )
    ranked = asset_search.search_records(db, groups)

    assert [record["_id"] for record in ranked] == ["exact", "expanded"]
    assert [record["match_tier"] for record in ranked] == ["original", "expanded"]


def test_legacy_search_result_has_empty_bilingual_keywords(db):
    seed(db, ("old", "dish.png", [], "", "", datetime.now(timezone.utc)))
    db[asset_index.RECORD_COLLECTION].update_one({"_id": "old"}, {"$unset": {"keywords": ""}})
    query = asset_index.save_query(db, query="", mode="literal", groups=[])

    page = asset_search.search_page(db, query["_id"], skip=0, limit=10)

    assert page["items"][0]["keywords"] == {"zh": [], "en": []}


def test_legacy_descriptions_do_not_participate_in_search(db):
    seed(db, ("old", "牛排.png", ["牛排"], "旧猪排描述", "Old pork chop description", datetime.now(timezone.utc)))
    db[asset_index.RECORD_COLLECTION].update_one(
        {"_id": "old"}, {"$set": {"keywords": {"zh": ["牛排"], "en": ["beef steak"]}}}
    )
    assert asset_search.search_records(db, asset_search.literal_groups("猪排")) == []
    assert asset_search.search_records(db, asset_search.literal_groups("pork chop")) == []


def test_semantic_vocabulary_marks_equivalents_as_original_and_expansions_as_lower(db):
    now = datetime.now(timezone.utc)
    seed(
        db,
        ("exact", "担担面.png", [], "", "", now),
        ("traditional", "擔擔麵.png", [], "", "", now - timedelta(minutes=1)),
        ("english", "noodles.png", [], "", "Dan dan noodles with chilli oil.", now - timedelta(minutes=2)),
        ("expanded", "麻辣面.png", [], "一碗麻辣拌面。", "", now + timedelta(minutes=1)),
    )
    groups = asset_search.validate_vocabulary(
        vocabulary(
            group("担担面", simplified="担担面", traditional="擔擔麵", english="dan dan noodles", expansions=["麻辣面"])
        ),
        "担担面",
    )
    document = asset_index.save_query(db, query="担担面", mode=asset_search.MODE_SEMANTIC, groups=groups)

    ranked = asset_search.search_records(db, document["groups"])

    assert [record["_id"] for record in ranked] == ["exact", "traditional", "english", "expanded"]
    assert [record["match_tier"] for record in ranked] == [
        asset_search.TIER_ORIGINAL,
        asset_search.TIER_ORIGINAL,
        asset_search.TIER_ORIGINAL,
        asset_search.TIER_EXPANDED,
    ]


def test_two_conditions_are_anded_and_can_be_satisfied_by_different_fields(db):
    now = datetime.now(timezone.utc)
    seed(
        db,
        ("both", "精品牛肉.png", ["火锅"], "", "", now),
        ("beef-only", "牛肉.png", [], "", "", now),
        ("hotpot-only", "番茄锅.png", ["火锅"], "", "", now),
    )
    groups = asset_search.validate_vocabulary(
        vocabulary(
            group("牛肉", simplified="牛肉", english="beef"),
            group("火锅", simplified="火锅", traditional="火鍋", english="hot pot"),
        ),
        "牛肉 火锅",
    )

    ranked = asset_search.search_records(db, groups)

    assert [record["_id"] for record in ranked] == ["both"]


def test_grouping_may_span_several_tokens(db):
    groups = asset_search.validate_vocabulary(
        vocabulary(group("dan dan noodles", simplified="担担面", traditional="擔擔麵", english="dan dan noodles")),
        "dan dan noodles",
    )
    assert len(groups) == 1
    assert groups[0]["input"] == "dan dan noodles"


def test_missing_or_unknown_group_is_rejected(db):
    with pytest.raises(AssetIndexError):
        asset_search.validate_vocabulary(
            vocabulary(group("牛肉", simplified="牛肉", english="beef")), "牛肉 火锅"
        )
    with pytest.raises(AssetIndexError):
        asset_search.validate_vocabulary(
            vocabulary(group("羊肉", simplified="羊肉", english="lamb")), "牛肉"
        )
    with pytest.raises(AssetIndexError):
        asset_search.validate_vocabulary({"groups": []}, "牛肉")


def test_expansions_are_deduped_and_limited_across_groups(db):
    groups = asset_search.validate_vocabulary(
        vocabulary(
            group("牛肉", simplified="牛肉", english="beef", expansions=[
                "牛排", "Beef", "肥牛", "牛腩", "牛舌", "牛柳", "牛腱", "牛尾", "beef brisket", "beef shank",
            ]),
            group("火锅", simplified="火锅", traditional="火鍋", english="hot pot", expansions=[
                "番茄锅", "麻辣锅", "清汤锅", "鸳鸯锅", "菌菇火锅", "牛油火锅", "tomato hot pot", "clear broth hot pot", "mushroom hot pot",
            ]),
            group("白盘", simplified="白盘", english="white plate", expansions=["白色盘子"]),
        ),
        "牛肉 火锅 白盘",
    )
    expansions = [term for item in groups for term in item["expansion_terms"]]
    assert "Beef" not in expansions  # already an equivalent of the same condition
    assert len(expansions) == 16
    assert [len(item["expansions"]) for item in groups] == [8, 8, 0]
    assert groups[0]["expansions"][-1] == "beef brisket"
    assert groups[1]["expansions"][-1] == "clear broth hot pot"
    assert all(item["expansions"] == item["expansion_terms"] for item in groups)


def test_more_expansions_match_full_phrases_without_losing_other_conditions(db):
    now = datetime.now(timezone.utc)
    seed(
        db,
        ("exact", "火锅白盘.png", [], "", "", now),
        ("expanded", "dish.png", ["白盘"], "", "clear broth hot pot", now),
        ("wrong-phrase", "dish.png", ["白盘"], "", "clear broth soup", now),
        ("missing-condition", "dish.png", [], "", "clear broth hot pot", now),
    )
    groups = asset_search.validate_vocabulary(
        vocabulary(
            group("火锅", simplified="火锅", traditional="火鍋", english="hot pot", expansions=[
                "番茄锅", "麻辣锅", "清汤锅", "鸳鸯锅", "菌菇火锅", "牛油火锅", "tomato hot pot", "clear broth hot pot",
            ]),
            group("白盘", simplified="白盘", english="white plate", expansions=["白色盘子"]),
        ),
        "火锅 白盘",
    )

    ranked = asset_search.search_records(db, groups)

    assert [record["_id"] for record in ranked] == ["exact", "expanded"]


def test_agent_failure_degrades_to_the_typed_words(db):
    client = FakeClient(CoreAIError("gateway_timeout", "网关超时"))

    document = asset_search.resolve_query(
        db, client=client, settings=FakeSettings(), query="牛肉", semantic=True
    )

    assert document["degraded"] is True
    assert document["mode"] == asset_search.MODE_LITERAL
    assert document["groups"][0]["input"] == "牛肉"
    assert client.calls == 1


def test_invalid_vocabulary_degrades_without_a_repair_call(db):
    client = FakeClient({"groups": [{"input": "牛肉", "equivalents": {}}]})

    document = asset_search.resolve_query(
        db, client=client, settings=FakeSettings(), query="牛肉", semantic=True
    )

    assert document["degraded"] is True
    assert client.calls == 1


def test_special_characters_and_phrases_match_literally(db):
    now = datetime.now(timezone.utc)
    seed(
        db,
        ("percent", "100%牛肉.png", [], "", "", now),
        ("phrase", "hot pot.png", [], "", "Hot pot set.", now),
    )
    literal = asset_index.save_query(
        db, query="100%", mode="literal", groups=asset_search.literal_groups("100%")
    )
    assert [record["_id"] for record in asset_search.search_records(db, literal["groups"])] == ["percent"]

    semantic = asset_index.save_query(
        db,
        query="hot pot",
        mode="semantic",
        groups=asset_search.validate_vocabulary(
            vocabulary(group("hot pot", simplified="火锅", traditional="火鍋", english="hot pot")),
            "hot pot",
        ),
    )
    assert [record["_id"] for record in asset_search.search_records(db, semantic["groups"])] == ["phrase"]


def test_paging_splits_a_ranked_list_without_duplicates(db):
    now = datetime.now(timezone.utc)
    rows = []
    for index in range(5):
        rows.append((f"orig-{index}", f"牛肉{index}.png", [], "", "", now - timedelta(minutes=index)))
    for index in range(3):
        rows.append((f"exp-{index}", f"菜品{index}.png", [], "麻辣小吃一份。", "", now + timedelta(minutes=index)))
    seed(db, *rows)
    document = asset_index.save_query(
        db,
        query="牛肉",
        mode="semantic",
        groups=asset_search.validate_vocabulary(
            vocabulary(group("牛肉", simplified="牛肉", english="beef", expansions=["麻辣小吃"])),
            "牛肉",
        ),
    )

    first = asset_search.search_page(db, document["_id"], skip=0, limit=5)
    second = asset_search.search_page(db, document["_id"], skip=5, limit=5)

    ids = [item["id"] for item in first["items"] + second["items"]]
    assert first["total"] == 8
    assert len(ids) == len(set(ids)) == 8
    assert {item["id"] for item in first["items"]} == {f"orig-{index}" for index in range(5)}
    assert {item["match_tier"] for item in second["items"]} == {asset_search.TIER_EXPANDED}


def test_empty_query_lists_everything_newest_first(db):
    now = datetime.now(timezone.utc)
    seed(
        db,
        ("old", "old.png", [], "", "", now - timedelta(days=1)),
        ("new", "new.png", [], "", "", now),
        ("undated", "undated.png", [], "", "", None),
    )
    document = asset_index.save_query(db, query="", mode="literal", groups=[])

    ranked = asset_search.search_records(db, document["groups"])

    assert [record["_id"] for record in ranked] == ["new", "old", "undated"]


def test_search_items_fall_back_to_the_file_endpoint(db):
    now = datetime.now(timezone.utc)
    seed(db, ("local", "火锅海报.png", ["火锅"], "一只火锅。", "A hot pot.", now))
    db.assets.insert_one(
        {
            "_id": "local",
            "original_name": "火锅海报.png",
            "width": 320,
            "height": 240,
            "mime_type": "image/png",
            "source": "upload",
            "prepared_path": "social-media/prepared/local.png",
            "created_at": now,
        }
    )
    document = asset_index.save_query(
        db, query="火锅", mode="literal", groups=asset_search.literal_groups("火锅")
    )

    page = asset_search.search_page(db, document["_id"], skip=0, limit=10, merchant_id="m1")

    # Local uploads keep no stored URL: without the fallback the card renders a broken image.
    assert page["items"][0]["url"] == "/api/merchants/m1/assets/local/file"
