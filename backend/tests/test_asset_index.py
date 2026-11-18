from __future__ import annotations

from datetime import datetime, timedelta, timezone

import mongomock
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.middleware import merchant_context_middleware
from app.routes import asset_index as asset_index_routes
from app.routes import merchant_context
from app.services import asset_index, asset_index_runner, asset_records, maintenance, storage
from app.services.core_ai import CoreAIError
from app.services.object_store import LocalObjectStore
from app.services.scoped_db import ScopedDatabase
from tests.conftest import png_bytes

ORIGIN = "http://127.0.0.1:5173"
WRITE_HEADERS = {"Origin": ORIGIN, "X-Media-Request": "1"}
MERCHANT = "m1"


def recognition(name="红烧肉砂锅", tags=("红烧肉", "砂锅", "浓汁"), zh="一只砂锅装着红烧肉。", en="A clay pot of braised pork."):
    return {
        "name": name,
        "tags": list(tags),
        "description_zh": zh,
        "description_en": en,
        "keywords": {"zh": ["红烧肉", "砂锅", "浓汁"], "en": ["braised pork", "clay pot", "thick sauce"]},
    }


class FakeCoreAI:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0
        self.messages = []

    def chat_json(self, messages, *, model=None, timeout_seconds=None):
        self.calls += 1
        self.messages.append(messages)
        value = self.responses.pop(0) if self.responses else recognition()
        if isinstance(value, Exception):
            raise value
        return value


def settings_for(tmp_path, **overrides):
    values = {
        "storage_dir": str(tmp_path / "storage"),
        "am_bypass_local": True,
        "social_media_origin": ORIGIN,
        "core_ai_api_key": "test-key",
        "asset_index_concurrency": 1,
        "asset_index_auto_describe": False,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def local(tmp_path):
    settings = settings_for(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = ScopedDatabase(mongomock.MongoClient().db, MERCHANT)
    return db, store, settings


def add_asset(db, store, settings, name="dish.png", *, tags=None, color=(180, 90, 50)):
    prepared = storage.prepare_upload(settings, store, png_bytes(color=color), name)
    document = asset_records.asset_document(prepared)
    if tags is not None:
        document["tags"] = list(tags)
    db.assets.insert_one(document)
    return document


def run(db, store, settings, client, mode, asset_ids):
    created, _ = asset_index.create_run(db, mode=mode, asset_ids=asset_ids)
    return asset_index_runner.run_index(
        db, settings, client, store, str(created["_id"]), merchant_id=MERCHANT
    )


def test_initialize_writes_name_tags_and_keywords_without_descriptions(local):
    db, store, settings = local
    asset = add_asset(db, store, settings, "dish.png", tags=["11"])
    client = FakeCoreAI(recognition())

    finished = run(db, store, settings, client, asset_index.RUN_INITIALIZE, [str(asset["_id"])])

    saved = db.assets.find_one({"_id": asset["_id"]})
    assert saved["original_name"] == "红烧肉砂锅.png"
    assert saved["tags"] == ["红烧肉", "砂锅", "浓汁"]
    record = asset_index.record_for_asset(db, str(asset["_id"]))
    assert record["description_status"] == asset_index.READY
    assert not record.get("description_zh")
    assert not record.get("description_en")
    assert record["keywords"] == recognition()["keywords"]
    assert record["name"] == "红烧肉砂锅.png"
    assert finished["counters"]["succeeded"] == 1
    # One image, one request: nothing extra is queued for the description.
    assert client.calls == 1


def test_initialize_keeps_original_values_when_the_model_answer_is_invalid(local):
    db, store, settings = local
    asset = add_asset(db, store, settings, "dish.png", tags=["11"])
    client = FakeCoreAI({"name": "红烧肉", "tags": ["只有两个", "标签"], "description_zh": "", "description_en": ""})

    finished = run(db, store, settings, client, asset_index.RUN_INITIALIZE, [str(asset["_id"])])

    saved = db.assets.find_one({"_id": asset["_id"]})
    assert saved["original_name"] == "dish.png"
    assert saved["tags"] == ["11"]
    assert finished["counters"]["failed"] == 1
    record = asset_index.record_for_asset(db, str(asset["_id"]))
    assert record["description_status"] == asset_index.FAILED
    assert not record.get("description_zh")
    assert "标签数量" in record["description_error"]


def test_retry_scope_only_contains_unfinished_assets(local):
    db, store, settings = local
    first = add_asset(db, store, settings, "a.png", color=(10, 20, 30))
    second = add_asset(db, store, settings, "b.png", color=(40, 50, 60))
    client = FakeCoreAI(recognition(), CoreAIError("gateway_timeout", "超时"))

    run(db, store, settings, client, asset_index.RUN_INITIALIZE, [str(first["_id"])])
    run(db, store, settings, client, asset_index.RUN_INITIALIZE, [str(second["_id"])])

    assert asset_index_runner.scope_for_mode(db, asset_index.RUN_REINDEX) != []
    assert asset_index_runner.scope_for_mode(db, "retry") == [str(second["_id"])]


def test_reindex_only_refreshes_keywords(local):
    db, store, settings = local
    asset = add_asset(db, store, settings, "dish.png")
    db.assets.update_one({"_id": asset["_id"]}, {"$set": {"original_name": "手工命名.png", "tags": ["人工"]}})
    client = FakeCoreAI(recognition(name="模型又想改名", tags=["甲", "乙", "丙"], zh="新描述", en="New description."))

    run(db, store, settings, client, asset_index.RUN_REINDEX, [str(asset["_id"])])

    saved = db.assets.find_one({"_id": asset["_id"]})
    assert saved["original_name"] == "手工命名.png"
    assert saved["tags"] == ["人工"]
    record = asset_index.record_for_asset(db, str(asset["_id"]))
    assert not record.get("description_zh")
    assert record["keywords"] == recognition()["keywords"]
    assert record["description_status"] == asset_index.READY


def test_invalid_keywords_keep_previous_keywords(local):
    db, store, settings = local
    asset = add_asset(db, store, settings)
    run(db, store, settings, FakeCoreAI(recognition()), asset_index.RUN_REINDEX, [str(asset["_id"])])
    previous = asset_index.record_for_asset(db, str(asset["_id"]))
    broken = recognition(zh="不应写入的新描述", en="Must not replace the old description.")
    broken["keywords"] = {"zh": ["新词"], "en": "not an array"}

    finished = run(db, store, settings, FakeCoreAI(broken), asset_index.RUN_REINDEX, [str(asset["_id"])])

    assert finished["counters"]["failed"] == 1
    record = asset_index.record_for_asset(db, str(asset["_id"]))
    assert record["keywords"] == previous["keywords"]


def test_reindex_uses_current_name_tags_and_replaces_old_pork_keywords(local):
    db, store, settings = local
    asset = add_asset(db, store, settings, "猪排.png", tags=["猪排", "白盘"])
    asset_index_runner.sync_catalog(db)
    db[asset_index.RECORD_COLLECTION].update_one(
        {"_id": asset["_id"]}, {"$set": {
            "keywords": {"zh": ["猪排"], "en": ["pork chop"]},
            "description_zh": "旧猪排描述", "description_en": "Old pork chop description.",
        }},
    )
    db.assets.update_one({"_id": asset["_id"]}, {"$set": {"original_name": "牛排.png", "tags": ["牛肉", "白盘"]}})
    asset_index_runner.sync_catalog(db)
    keywords = {"zh": ["牛排", "牛肉", "白盘"], "en": ["beef steak", "beef", "white plate"]}
    client = FakeCoreAI({"keywords": keywords})

    finished = run(db, store, settings, client, asset_index.RUN_REINDEX, [str(asset["_id"])])

    assert finished["counters"]["succeeded"] == 1
    assert asset_index.record_for_asset(db, str(asset["_id"]))["keywords"] == keywords
    messages = client.messages[0]
    assert "当前名称：牛排.png；当前标签：牛肉、白盘" in messages[1]["content"][0]["text"]
    assert "旧猪排描述" not in str(messages)
    assert client.calls == 1
    from app.services import asset_search
    assert asset_search.search_records(db, asset_search.literal_groups("猪排")) == []
    assert asset_search.search_records(db, asset_search.literal_groups("beef steak"))


@pytest.mark.parametrize("query", ["白盘", "white plate"])
def test_routes_match_and_return_keyword_only_hits(monkeypatch, tmp_path, query):
    client, raw, store, settings, _spawned = _build_web(monkeypatch, tmp_path)
    db = ScopedDatabase(raw, MERCHANT)
    asset = add_asset(db, store, settings)
    asset_index_runner.sync_catalog(db)
    keywords = {"zh": ["白盘"], "en": ["white plate"]}
    db[asset_index.RECORD_COLLECTION].update_one({"_id": asset["_id"]}, {"$set": {"keywords": keywords}})

    search = client.post(
        "/api/merchants/m1/asset-searches", headers=WRITE_HEADERS,
        json={"query": query, "semantic": False},
    )
    assert search.status_code == 201, search.text
    assert search.json()["total"] == 1
    page = client.get(f"/api/merchants/m1/am-assets?search_id={search.json()['id']}")
    assert page.status_code == 200, page.text
    assert page.json()["items"][0]["keywords"] == keywords


def test_concurrent_rename_during_recognition_is_not_overwritten(local, monkeypatch):
    db, store, settings = local
    asset = add_asset(db, store, settings, "dish.png")

    import app.services.asset_index_runner as runner_module
    from app.services import asset_recognition

    def rename_midway(_client, **_kwargs):
        db.assets.update_one(
            {"_id": asset["_id"]},
            {"$set": {"original_name": "运营改名.png", "name_updated_at": datetime.now(timezone.utc)}},
        )
        return asset_recognition.validate_recognition(recognition())

    monkeypatch.setattr(runner_module.asset_recognition, "recognize", rename_midway)

    finished = run(db, store, settings, FakeCoreAI(), asset_index.RUN_INITIALIZE, [str(asset["_id"])])

    saved = db.assets.find_one({"_id": asset["_id"]})
    assert saved["original_name"] == "运营改名.png"
    assert finished["counters"]["failed"] == 1
    assert "修改" in finished["errors"][0]["message"]


def test_newer_description_request_wins_over_a_slow_result(local):
    db, store, settings = local
    asset = add_asset(db, store, settings, "dish.png")
    asset_index.request_description(db, asset)
    claimed = asset_index.claim_record(db, str(asset["_id"]), "arun_old")
    stale_version = claimed["description_version"]
    # The operator edits the tags while the model is still reading the image.
    asset_index.request_description(db, db.assets.find_one({"_id": asset["_id"]}))

    applied = asset_index.save_keywords(
        db, str(asset["_id"]), version=stale_version, keywords={"zh": ["旧"], "en": ["old"]}
    )

    assert applied is False
    record = asset_index.record_for_asset(db, str(asset["_id"]))
    assert record["keywords"] == {"zh": [], "en": []}
    assert record["description_status"] == asset_index.PENDING


def test_failed_keyword_generation_keeps_the_previous_keywords(local):
    db, store, settings = local
    asset = add_asset(db, store, settings, "dish.png")
    asset_index.request_description(db, asset)
    record = asset_index.record_for_asset(db, str(asset["_id"]))
    asset_index.save_keywords(
        db, str(asset["_id"]), version=record["description_version"],
        keywords={"zh": ["旧词"], "en": ["old term"]},
    )
    asset_index.request_description(db, db.assets.find_one({"_id": asset["_id"]}))
    newer = asset_index.record_for_asset(db, str(asset["_id"]))

    asset_index.fail_record(db, str(asset["_id"]), version=newer["description_version"], error="模型超时")

    record = asset_index.record_for_asset(db, str(asset["_id"]))
    assert record["description_status"] == asset_index.FAILED
    assert record["keywords"] == {"zh": ["旧词"], "en": ["old term"]}


def test_deleted_asset_drops_its_record(local):
    db, store, settings = local
    asset = add_asset(db, store, settings, "dish.png")
    asset_index.request_description(db, asset)
    db.assets.delete_one({"_id": asset["_id"]})

    finished = run(db, store, settings, FakeCoreAI(), asset_index.RUN_REINDEX, [str(asset["_id"])])

    assert asset_index.record_for_asset(db, str(asset["_id"])) is None
    assert finished["counters"]["skipped"] == 1


def test_catalog_sync_mirrors_names_and_removes_stale_records(local):
    db, store, settings = local
    first = add_asset(db, store, settings, "a.png", color=(1, 2, 3))
    second = add_asset(db, store, settings, "b.png", color=(4, 5, 6))
    asset_index_runner.sync_catalog(db)
    assert asset_index.summary(db)["total"] == 2

    db.assets.delete_one({"_id": second["_id"]})
    db.assets.update_one({"_id": first["_id"]}, {"$set": {"original_name": "新名字.png"}})
    report = asset_index_runner.sync_catalog(db)

    assert report["removed"] == 1
    assert asset_index.record_for_asset(db, str(first["_id"]))["name"] == "新名字.png"
    assert asset_index.record_for_asset(db, str(second["_id"])) is None


def test_second_click_joins_the_active_run(local):
    db, store, settings = local
    asset = add_asset(db, store, settings, "dish.png")

    first, created = asset_index.create_run(db, mode=asset_index.RUN_REINDEX, asset_ids=[str(asset["_id"])])
    second, again = asset_index.create_run(db, mode=asset_index.RUN_REINDEX, asset_ids=[str(asset["_id"])])

    assert created is True
    assert again is False
    assert second["_id"] == first["_id"]


def test_interrupted_run_releases_claims_and_requeues(local):
    db, store, settings = local
    asset = add_asset(db, store, settings, "dish.png")
    asset_index.request_description(db, asset)
    created, _ = asset_index.create_run(db, mode=asset_index.RUN_REINDEX, asset_ids=[str(asset["_id"])])
    asset_index.mark_started(db, str(created["_id"]))
    asset_index.claim_record(db, str(asset["_id"]), str(created["_id"]))

    interrupted = asset_index.interrupt_active_runs(db)

    assert interrupted == [str(created["_id"])]
    record = asset_index.record_for_asset(db, str(asset["_id"]))
    assert record["description_status"] == asset_index.PENDING
    assert record["claim_run_id"] is None


def _build_web(monkeypatch, tmp_path):
    raw = mongomock.MongoClient().db
    raw.merchants.insert_one({"_id": MERCHANT, "name": "Imported Merchant"})
    maintenance.initialize(raw)
    settings = settings_for(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    web = FastAPI()
    web.state.raw_db = raw
    web.state.db = raw
    web.state.settings = settings
    web.state.local_store = store
    web.state.store = store
    web.state.core_ai = FakeCoreAI()
    web.middleware("http")(merchant_context_middleware)
    router = APIRouter(prefix="/api/merchants/{merchant_id}")
    router.include_router(asset_index_routes.router)
    router.include_router(merchant_context.router)
    web.include_router(router)

    from fastapi.responses import JSONResponse

    @web.exception_handler(asset_index.AssetIndexError)
    async def _index_error(_, exc):  # noqa: ANN001
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": exc.message}})

    spawned: list[str] = []
    monkeypatch.setattr(
        asset_index_runner,
        "_spawn",
        lambda *args, **kwargs: spawned.append(str(args[4])),
    )
    return TestClient(web), raw, store, settings, spawned


def test_routes_report_status_create_a_run_and_search(monkeypatch, tmp_path):
    client, raw, store, settings, spawned = _build_web(monkeypatch, tmp_path)
    db = ScopedDatabase(raw, MERCHANT)
    asset = add_asset(db, store, settings, "dish.png", tags=["火锅"])
    asset_index_runner.sync_catalog(db)
    record = asset_index.record_for_asset(db, str(asset["_id"]))
    asset_index.save_keywords(
        db, str(asset["_id"]), version=record["description_version"],
        keywords={"zh": ["牛肉"], "en": ["beef"]},
    )

    created = client.post(
        "/api/merchants/m1/asset-index-runs", headers=WRITE_HEADERS, json={"mode": "reindex"}
    )
    assert created.status_code == 201, created.text
    assert created.json()["mode"] == "reindex"
    assert spawned

    status = client.get("/api/merchants/m1/asset-index").json()
    assert status["total"] == 1
    assert status["described"] == 1

    search = client.post(
        "/api/merchants/m1/asset-searches",
        headers=WRITE_HEADERS,
        json={"query": "beef", "semantic": False},
    )
    assert search.status_code == 201, search.text
    body = search.json()
    assert body["mode"] == "literal"
    assert body["total"] == 1

    page = client.get(f"/api/merchants/m1/am-assets?search_id={body['id']}&limit=10").json()
    assert [item["id"] for item in page["items"]] == [str(asset["_id"])]
    assert page["items"][0]["keywords"] == {"zh": ["牛肉"], "en": ["beef"]}
    assert "description_en" not in page["items"][0]
    assert page["items"][0]["match_tier"] == "original"


def test_expired_search_is_reported_instead_of_calling_the_model(monkeypatch, tmp_path):
    client, raw, store, settings, _spawned = _build_web(monkeypatch, tmp_path)
    db = ScopedDatabase(raw, MERCHANT)
    add_asset(db, store, settings, "dish.png")
    asset_index_runner.sync_catalog(db)
    body = client.post(
        "/api/merchants/m1/asset-searches", headers=WRITE_HEADERS, json={"query": "牛肉"}
    ).json()
    raw[asset_index.QUERY_COLLECTION].update_one(
        {"_id": body["id"]}, {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)}}
    )

    response = client.get(f"/api/merchants/m1/am-assets?search_id={body['id']}")

    assert response.status_code == 410
    assert response.json()["error"]["code"] == "search_expired"


def test_description_queued_during_a_run_is_picked_up_afterwards(local, monkeypatch):
    db, store, settings = local
    busy = add_asset(db, store, settings, "a.png", color=(11, 22, 33))
    edited = add_asset(db, store, settings, "b.png", color=(44, 55, 66))
    spawned: list[str] = []
    monkeypatch.setattr(asset_index_runner, "_spawn", lambda *args, **kwargs: spawned.append(str(args[4])))

    active, _created = asset_index.create_run(
        db, mode=asset_index.RUN_INITIALIZE, asset_ids=[str(busy["_id"])]
    )
    asset_index.mark_started(db, str(active["_id"]))
    asset_index_runner.describe_asset(
        db, settings, FakeCoreAI(), store,
        merchant_id=MERCHANT, asset_id=str(edited["_id"]),
    )

    # The click joined the live task instead of starting a second one.
    assert spawned == []
    assert asset_index.record_for_asset(db, str(edited["_id"]))["description_status"] == asset_index.PENDING

    asset_index_runner.run_index(db, settings, FakeCoreAI(), store, str(active["_id"]))

    # Once the busy task ends, the queued description gets its own run.
    assert spawned, "follow-up run was not started"


def test_retry_run_only_covers_failed_assets(monkeypatch, tmp_path):
    client, raw, store, settings, spawned = _build_web(monkeypatch, tmp_path)
    db = ScopedDatabase(raw, MERCHANT)
    good = add_asset(db, store, settings, "good.png", color=(7, 8, 9))
    broken = add_asset(db, store, settings, "broken.png", color=(70, 80, 90))
    asset_index_runner.sync_catalog(db)
    ready = asset_index.record_for_asset(db, str(good["_id"]))
    asset_index.save_keywords(
        db, str(good["_id"]), version=ready["description_version"],
        keywords={"zh": ["已完成"], "en": ["completed"]},
    )
    failed = asset_index.record_for_asset(db, str(broken["_id"]))
    asset_index.fail_record(db, str(broken["_id"]), version=failed["description_version"], error="超时")

    response = client.post(
        "/api/merchants/m1/asset-index-runs", headers=WRITE_HEADERS, json={"mode": "retry"}
    )

    assert response.status_code == 201, response.text
    assert response.json()["mode"] == "retry"
    run = raw[asset_index.RUN_COLLECTION].find_one({"mode": "retry"})
    assert run["scope"] == [str(broken["_id"])]
    assert spawned
