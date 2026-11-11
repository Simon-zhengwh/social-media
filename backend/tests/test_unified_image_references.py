from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import mongomock
import pytest
from fastapi import BackgroundTasks, HTTPException
from pydantic import ValidationError

from app.routes import whole_image as routes
from app.routes import prompts as prompt_routes
from app.schemas import WholeImageTaskCreate, WholeImageEditRequest, PromptOptimizeRequest
from app.services import whole_image as service, prompt_optimizer, storage
from app.services.scoped_db import ScopedDatabase
from tests.test_whole_image import FakeCoreAI, _request


def asset(db, settings, store, data, name):
    prepared = storage.prepare_upload(settings, store, data, name)
    db.assets.insert_one({
        "_id": prepared.id, "original_name": name, "path": prepared.path,
        "prepared_path": prepared.prepared_path, "mime_type": prepared.mime_type,
        "width": prepared.width, "height": prepared.height,
    })
    return prepared.id


@pytest.mark.parametrize("schema", [WholeImageTaskCreate, PromptOptimizeRequest])
def test_unified_reference_schema_is_opt_in_and_validates_ids(schema):
    assert schema(prompt="test").reference_asset_ids is None
    assert schema(prompt="test", reference_asset_ids=[]).reference_asset_ids == []
    assert schema(prompt="test", reference_asset_ids=[" a ", "b"]).reference_asset_ids == ["a", "b"]
    for ids in [["a"] * 2, [""], [f"image-{i}" for i in range(7)]]:
        with pytest.raises(ValidationError):
            schema(prompt="test", reference_asset_ids=ids)
    assert WholeImageEditRequest(instruction="test", source_version_id="version").reference_mode == "legacy"
    with pytest.raises(ValidationError):
        WholeImageEditRequest(instruction="test", source_version_id="version", reference_mode="other")


@pytest.mark.parametrize("ids", [["same", " same "], [""], [f"image-{i}" for i in range(6)]])
def test_unified_edit_rejects_invalid_reference_numbering(ids):
    with pytest.raises(ValidationError):
        WholeImageEditRequest(
            instruction="test", source_version_id="version", reference_mode="unified",
            reference_asset_ids=ids,
        )


@pytest.mark.parametrize("operation", ["generate", "optimize", "edit"])
def test_unified_routes_reject_asset_aliases_that_collapse_image_numbers(
    settings, store, product_png, monkeypatch, operation,
):
    db = mongomock.MongoClient().db
    first = asset(db, settings, store, product_png, "one.png")
    req = _request(db, settings, store, FakeCoreAI())
    monkeypatch.setattr(routes.request_assets, "resolve_asset_ids", lambda request, ids: [first] if ids else [])
    db.whole_tasks.insert_one({
        "_id": "task", "status": "succeeded", "input": {"image_model": "gpt-image-2.5-sunburst"},
        "created_at": datetime.now(timezone.utc),
    })
    db.whole_versions.insert_one({"_id": "version", "task_id": "task"})
    with pytest.raises(HTTPException) as exc:
        if operation == "generate":
            routes.create_task(WholeImageTaskCreate(
                prompt="@图片1 和 @图片2", reference_asset_ids=[first, "external-alias"],
            ), req, BackgroundTasks())
        elif operation == "optimize":
            prompt_routes.optimize_prompt(PromptOptimizeRequest(
                prompt="@图片1 和 @图片2", reference_asset_ids=[first, "external-alias"],
            ), req)
        else:
            routes.create_edit("task", WholeImageEditRequest(
                instruction="@图片2 和 @图片3", source_version_id="version", reference_mode="unified",
                reference_asset_ids=[first, "external-alias"],
            ), req, BackgroundTasks())
    assert exc.value.status_code == 422
    assert "重复" in exc.value.detail


@pytest.mark.parametrize("optimized", [False, True])
def test_unified_generation_uses_manual_order_and_unclassified_roles(settings, store, product_png, logo_png, optimized):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    first = asset(db, settings, store, product_png, "reference-one.png")
    second = asset(db, settings, store, fake._image((80, 90, 100)), "reference-two.png")
    logo = asset(db, settings, store, logo_png, "logo.png")
    brand_ref = asset(db, settings, store, fake._image((90, 50, 100)), "brand-style.png")
    req = _request(db, settings, store, fake)
    db.brands.update_one({"_id": "seejoy"}, {"$set": {"reference_asset_ids": [brand_ref]}})
    created = routes.create_task(WholeImageTaskCreate(
        prompt="把 @图片1 的背景换为 @图片2", reference_asset_ids=[second, first],
        logo_asset_id=logo, brand_mode="preset", brand_id="seejoy", prompt_optimized=optimized,
    ), req, BackgroundTasks())
    assert created["input"]["reference_asset_ids"] == [second, first]
    service.run_generation(db, settings, store, fake, created["id"])
    call = fake.edit_calls[-1]
    assert call["reference_names"] == ["reference-two.png", "reference-one.png", "logo.png"] + ([] if optimized else ["brand-style.png"])
    assert "图片1" in call["prompt"] and "图片2" in call["prompt"]
    assert "用户参考图" in call["prompt"]
    assert "用途由用户要求" in call["prompt"]
    assert "内容素材，主体及身份以这些图为准" not in call["prompt"]
    saved = db.whole_tasks.find_one({"_id": created["id"]})
    ids = [image["asset_id"] for image in saved["model_input_latest"]["images"]]
    assert ids == [second, first, logo] + ([] if optimized else [brand_ref])
    assert [image["role"] for image in saved["model_input_latest"]["images"]][:2] == ["reference", "reference"]


def test_unified_refs_are_validated_in_current_merchant(settings, store, product_png):
    raw = mongomock.MongoClient().db
    other = ScopedDatabase(raw, "other")
    foreign = asset(other, settings, store, product_png, "foreign.png")
    req = _request(raw, settings, store, FakeCoreAI())
    req.state = SimpleNamespace(db=ScopedDatabase(raw, "merchant"), merchant_id="merchant")
    with pytest.raises(HTTPException) as exc:
        routes.create_task(WholeImageTaskCreate(prompt="test", reference_asset_ids=[foreign]), req, BackgroundTasks())
    assert exc.value.status_code == 422


def test_unified_edit_starts_with_current_poster_then_added_refs_and_preserves_mode_on_retry(settings, store, product_png):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    original = asset(db, settings, store, product_png, "original.png")
    added = asset(db, settings, store, fake._image((10, 30, 50)), "this-turn.png")
    req = _request(db, settings, store, fake)
    created = routes.create_task(WholeImageTaskCreate(
        prompt="test", reference_asset_ids=[original], image_model="seedream-5.5-image-to-image",
    ), req, BackgroundTasks())
    service.run_generation(db, settings, store, fake, created["id"])
    source = db.whole_versions.find_one({"task_id": created["id"], "version": 1})
    source_asset = db.assets.find_one({"_id": source["output_asset_id"]})
    background = BackgroundTasks()
    response = routes.create_edit(created["id"], WholeImageEditRequest(
        instruction="把 @图片1 的背景换成 @图片2", source_version_id=source["_id"],
        reference_asset_ids=[added], reference_mode="unified",
    ), req, background)
    assert response["edit"]["reference_mode"] == "unified"
    background.tasks[0].func(*background.tasks[0].args, **background.tasks[0].kwargs)
    call = fake.edit_calls[-1]
    assert call["model"] == "seedream-5.5-image-to-image"
    assert call["reference_names"] == [source_asset["original_name"], "this-turn.png", "original.png"]
    assert "图片1" in call["prompt"] and "当前海报" in call["prompt"]
    latest = db.whole_tasks.find_one({"_id": created["id"]})
    assert [image["asset_id"] for image in latest["model_input_latest"]["images"]] == [source_asset["_id"], added, original]
    db.whole_tasks.update_one({"_id": created["id"]}, {"$set": {"edit.status": "failed"}})
    retry = BackgroundTasks()
    routes.retry_task(created["id"], req, retry)
    assert retry.tasks[0].kwargs.get("reference_mode") == "unified" or retry.tasks[0].args[-1] == "unified"


def test_optimizer_numbers_unclassified_manual_references(settings, store, product_png):
    db = mongomock.MongoClient().db
    first = asset(db, settings, store, product_png, "one.png")
    second = asset(db, settings, store, FakeCoreAI()._image((40, 70, 90)), "two.png")
    for asset_id in [first, second]:
        db.assets.update_one({"_id": asset_id}, {"$set": {"prepared_url": f"https://images.example/{asset_id}.png"}})
    class Agent:
        def run_agent(self, agent_id, input_text, **kwargs):
            self.input = input_text
            return json.dumps({"prompt": "把 @图片1 的主体替换成 @图片2"}, ensure_ascii=False)
    agent = Agent()
    output = prompt_optimizer.optimize_prompt(db, settings, store, agent, request=PromptOptimizeRequest(
        prompt="把 @图片1 的主体替换成 @图片2", reference_asset_ids=[second, first],
    ))
    assert output == "把 @图片1 的主体替换成 @图片2"
    manifest = json.loads(agent.input.split("图片清单（JSON）：\n", 1)[1])
    assert [image["label"] for image in manifest["reference_images"]] == ["图片1", "图片2"]
    assert manifest["reference_images"][0]["url"].endswith(f"/{second}.png")
    assert manifest["content_images"] == []
    assert list(manifest)[:3] == ["reference_images", "logo_images", "style_images"]
    assert "用途由用户要求" in agent.input
    assert "@图片" in agent.input and "编号" in agent.input
