from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import mongomock
import pytest
from fastapi import BackgroundTasks, HTTPException
from PIL import Image
from pydantic import ValidationError

from app.routes import whole_image as whole_routes
from app.schemas import WholeImageEditRequest, WholeImageTaskCreate
from app.services import storage, whole_image as whole_service
from app.services.core_ai import CoreAIError


class FakeCoreAI:
    configured = True

    def __init__(self) -> None:
        self.generation_calls: list[dict] = []
        self.edit_calls: list[dict] = []
        self.fail_edit = False
        self._color_index = 0

    def _image(self, color: tuple[int, int, int]) -> bytes:
        buffer = io.BytesIO()
        Image.new("RGB", (640, 640), color).save(buffer, format="PNG")
        return buffer.getvalue()

    def generate_image(self, prompt: str, *, size: str, model: str | None = None) -> bytes:
        self.generation_calls.append({"prompt": prompt, "size": size, "model": model})
        colors = [(50, 90, 130), (210, 90, 25), (30, 120, 70)]
        color = colors[self._color_index % len(colors)]
        self._color_index += 1
        return self._image(color)

    def edit_image(self, prompt: str, references, *, size: str, model: str | None = None) -> bytes:
        reference_paths = [getattr(reference, "path", reference) for reference in references]
        self.edit_calls.append(
            {
                "prompt": prompt,
                "size": size,
                "model": model,
                "reference_bytes": [path.read_bytes() for path in reference_paths],
                "reference_names": [
                    getattr(reference, "name", Path(path).name)
                    for reference, path in zip(references, reference_paths)
                ],
            }
        )
        if self.fail_edit:
            raise CoreAIError("edit_failed", "模拟编辑失败")
        self._color_index += 1
        return self._image((180, 80, 40))

    def chat_json(self, messages, *, model=None, timeout_seconds=None):
        return {"status": "pass", "violations": []}


def _seed_default_brand(db) -> None:
    if db.brands.find_one({"_id": "seejoy"}) is not None:
        return
    now = datetime.now(timezone.utc)
    db.brands.insert_one(
        {
            "_id": "seejoy",
            "name": "SEEJOY 喜聚",
            "short_name": "喜聚",
            "summary": "test brand",
            "is_default": True,
            "layout_guide": "TEST LAYOUT GUIDE",
            "background_guide": "TEST BACKGROUND GUIDE",
            "logo_keywords": ["seejoy"],
            "cover_asset_id": None,
            "reference_asset_ids": [],
            "created_at": now,
            "updated_at": now,
        }
    )


def _request(db, settings, store, client):
    _seed_default_brand(db)
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                db=db,
                settings=settings,
                store=store,
                core_ai=client,
            )
        )
    )


def _create(
    db,
    settings,
    store,
    client,
    prompt="夏日冰美式海报",
    product_asset_ids=None,
    image_model="gpt-image-2.5-sunburst",
):
    return whole_routes.create_task(
        WholeImageTaskCreate(
            prompt=prompt,
            ratio="1:1",
            product_asset_ids=product_asset_ids or [],
            image_model=image_model,
        ),
        _request(db, settings, store, client),
        BackgroundTasks(),
    )


def _generate(db, settings, store, client, prompt="夏日冰美式海报"):
    created = _create(db, settings, store, client, prompt)
    whole_service.run_generation(db, settings, store, client, created["id"])
    task = db.whole_tasks.find_one({"_id": created["id"]})
    return created["id"], task


@pytest.mark.parametrize("optimized", [False, True])
def test_common_rules_snapshot_is_immutable_for_generation(settings, store, optimized):
    from app.services import brand_rules

    db = mongomock.MongoClient().db
    client = FakeCoreAI()
    original = dict(brand_rules.DEFAULT_RULES, copy_accuracy="SNAPSHOT_RULE")
    db.brand_rules.insert_one({"_id": "default", **original})
    created = whole_routes.create_task(
        WholeImageTaskCreate(prompt="测试", brand_mode="none", prompt_optimized=optimized),
        _request(db, settings, store, client), BackgroundTasks(),
    )
    db.brand_rules.update_one({"_id": "default"}, {"$set": {"copy_accuracy": "NEW_RULE"}})
    whole_service.run_generation(db, settings, store, client, created["id"])
    task = db.whole_tasks.find_one({"_id": created["id"]})
    assert task["brand_rules"] == original
    assert "SNAPSHOT_RULE" in client.generation_calls[0]["prompt"]
    assert "NEW_RULE" not in client.generation_calls[0]["prompt"]


def test_structured_brand_generation_uses_fields_once(settings, store):
    db = mongomock.MongoClient().db
    client = FakeCoreAI()
    req = _request(db, settings, store, client)
    db.brands.update_one({"_id": "seejoy"}, {"$set": {
        "schema_version": 2,
        "style_fields": {"palette_notes": "绿", "typography": "UNIQUE_FONT", "subject_style": "实拍", "background": "纸感"},
        "layout_guide": "LEGACY_CONTENT", "background_guide": "OLD_BACKGROUND",
    }})
    created = whole_routes.create_task(WholeImageTaskCreate(prompt="测试", brand_id="seejoy"), req, BackgroundTasks())
    whole_service.run_generation(db, settings, store, client, created["id"])
    prompt = client.generation_calls[0]["prompt"]
    assert prompt.count("UNIQUE_FONT") == 1
    assert "LEGACY_CONTENT" not in prompt


def test_historical_generation_does_not_fetch_new_common_rules(settings, store):
    db = mongomock.MongoClient().db
    client = FakeCoreAI()
    created = _create(db, settings, store, client)
    db.whole_tasks.update_one({"_id": created["id"]}, {"$unset": {"brand_rules": ""}})
    from app.services import brand_rules
    db.brand_rules.insert_one({"_id": "default", **brand_rules.DEFAULT_RULES, "copy_accuracy": "NOT_FOR_OLD_TASK"})
    whole_service.run_generation(db, settings, store, client, created["id"])
    assert "NOT_FOR_OLD_TASK" not in client.generation_calls[0]["prompt"]


def _seed_product(db, settings, store, data, name):
    prepared = storage.prepare_upload(settings, store, data, name, "product")
    db.assets.insert_one(
        {
            "_id": prepared.id,
            "kind": prepared.kind,
            "original_name": prepared.original_name,
            "path": prepared.path,
            "prepared_path": prepared.prepared_path,
            "mime_type": prepared.mime_type,
            "width": prepared.width,
            "height": prepared.height,
            "bytes": prepared.bytes,
            "sha256": prepared.sha256,
            "created_at": datetime.now(timezone.utc),
        }
    )
    return prepared


def _seed_style_reference(db, settings, store, data, name):
    prepared = storage.prepare_upload(settings, store, data, name, "brand_reference")
    db.assets.insert_one(
        {
            "_id": prepared.id,
            "kind": "brand_reference",
            "original_name": prepared.original_name,
            "path": prepared.path,
            "prepared_path": prepared.prepared_path,
            "mime_type": prepared.mime_type,
            "width": prepared.width,
            "height": prepared.height,
            "bytes": prepared.bytes,
            "sha256": prepared.sha256,
            "created_at": datetime.now(timezone.utc),
        }
    )
    return prepared


def test_generation_creates_v1_without_parent(settings, store):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    task_id, task = _generate(db, settings, store, fake)

    versions = list(db.whole_versions.find({"task_id": task_id}))
    assert task["status"] == "succeeded"
    assert task["current_version"] == 1
    assert task["selected_version_id"] == versions[0]["_id"]
    assert task["platform"] is None
    assert "发布平台未指定" in fake.generation_calls[0]["prompt"]
    assert len(versions) == 1
    assert versions[0]["parent_version_id"] is None
    assert versions[0]["operation"] == "generate"
    assert fake.generation_calls[0]["prompt"].endswith("夏日冰美式海报")
    assert "input_images" not in fake.generation_calls[0]
    assert versions[0]["model_input"]["endpoint"] == "/images/generations"


def test_create_without_platform_uses_selected_ratio(settings, store):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()

    created = whole_routes.create_task(
        WholeImageTaskCreate(prompt="自由设计", ratio="4:5"),
        _request(db, settings, store, fake),
        BackgroundTasks(),
    )

    assert created["platform"] is None
    assert created["placement"] is None
    assert created["input"]["platform_id"] is None
    assert created["input"]["ratio"] == "4:5"
    assert created["canvas"] == {"width": 1080, "height": 1350}


def test_generation_exposes_model_input_snapshot_with_reference_preview(
    settings, store, product_png
):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    product = _seed_product(db, settings, store, product_png, "dish.png")
    created = _create(
        db,
        settings,
        store,
        fake,
        prompt="展示新品",
        product_asset_ids=[product.id],
    )

    whole_service.run_generation(db, settings, store, fake, created["id"])

    version = db.whole_versions.find_one({"task_id": created["id"], "version": 1})
    snapshot = version["model_input"]
    assert snapshot == {
        "endpoint": "/images/edits",
        "model": "gpt-image-2.5-sunburst",
        "prompt": fake.edit_calls[0]["prompt"],
        "parameters": {
            "size": fake.edit_calls[0]["size"],
            "quality": settings.core_ai_image_quality,
            "n": 1,
            "output_format": "png",
        },
        "images": [
            {"asset_id": product.id, "name": "dish.png", "kind": "product"}
        ],
    }

    task_out = whole_routes.task_out(
        db, db.whole_tasks.find_one({"_id": created["id"]})
    )
    assert task_out["model_input"]["images"] == [
        {
            "asset_id": product.id,
            "name": "dish.png",
            "kind": "product",
            "preview_url": f"/api/assets/{product.id}/file",
        }
    ]


def test_generation_separates_subject_images_from_style_only_references(
    settings, store, product_png
):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    product = _seed_product(db, settings, store, product_png, "person.png")
    style_reference = _seed_style_reference(
        db, settings, store, product_png, "style-with-person.png"
    )
    created = whole_routes.create_task(
        WholeImageTaskCreate(
            prompt="把素材中的人物融合进海报",
            ratio="1:1",
            product_asset_ids=[product.id],
            brand_mode="reference",
            style_reference_asset_ids=[style_reference.id],
        ),
        _request(db, settings, store, fake),
        BackgroundTasks(),
    )

    whole_service.run_generation(db, settings, store, fake, created["id"])

    prompt = fake.edit_calls[0]["prompt"]
    assert "Subject/content reference image(s): 1." in prompt
    assert "Style-only reference image(s): 2." in prompt
    assert (
        "Never reuse people, faces, identities, poses, clothing, or subjects from style references"
        in prompt
    )
    assert fake.edit_calls[0]["reference_names"] == [
        "person.png",
        "style-with-person.png",
    ]


def test_optimized_prompt_is_sent_without_duplicate_style_context(
    settings, store, product_png
):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    product = _seed_product(db, settings, store, product_png, "person.png")
    style = _seed_style_reference(db, settings, store, product_png, "style.png")
    _seed_default_brand(db)
    db.brands.update_one({"_id": "seejoy"}, {"$set": {"reference_asset_ids": [style.id]}})
    optimized = "完整优化提示词：深绿色品牌海报，保留人物身份，标题『粉丝专享』。"
    created = whole_routes.create_task(
        WholeImageTaskCreate(
            prompt=optimized,
            prompt_optimized=True,
            product_asset_ids=[product.id],
            brand_id="seejoy",
            brand_mode="preset",
            platform_id="xiaohongshu",
        ),
        _request(db, settings, store, fake),
        BackgroundTasks(),
    )

    whole_service.run_generation(db, settings, store, fake, created["id"])

    task = db.whole_tasks.find_one({"_id": created["id"]})
    sent = fake.edit_calls[0]
    assert task["input"]["prompt_optimized"] is True
    assert sent["reference_names"] == ["person.png"]
    assert sent["prompt"].endswith(optimized)
    assert "TEST LAYOUT GUIDE" not in sent["prompt"]
    assert "TEST BACKGROUND GUIDE" not in sent["prompt"]
    assert "Style-only reference image" not in sent["prompt"]
    assert "following the user's prompt and the assigned roles of attached images" in sent["prompt"]
    assert "Text section" not in sent["prompt"]
    assert "1080×1440" in sent["prompt"]
    assert "禁止出现地址、电话号码或二维码" in sent["prompt"]
    assert "商家地址和联系电话" not in sent["prompt"]
    assert task["model_input_latest"]["images"] == [
        {"asset_id": product.id, "name": "person.png", "kind": "product"}
    ]


@pytest.mark.parametrize("optimized", [False, True])
def test_generation_does_not_impose_a_global_decoration_limit(settings, store, optimized):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    created = whole_routes.create_task(
        WholeImageTaskCreate(prompt="自由排版，价格和粉丝专享也要有设计感", prompt_optimized=optimized),
        _request(db, settings, store, fake),
        BackgroundTasks(),
    )
    whole_service.run_generation(db, settings, store, fake, created["id"])

    sent = fake.generation_calls[0]["prompt"]
    assert "at most one small auxiliary decorative accent" not in sent
    assert "Do not add unrequested written stickers" not in sent
    assert "Do not add other slogans, handwritten captions, notes, written stickers" not in sent


def test_failed_generation_exposes_the_attempted_model_input(settings, store, product_png):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    product = _seed_product(db, settings, store, product_png, "dish.png")
    fake.fail_edit = True
    created = _create(
        db,
        settings,
        store,
        fake,
        product_asset_ids=[product.id],
    )

    whole_service.run_generation(db, settings, store, fake, created["id"])

    task = db.whole_tasks.find_one({"_id": created["id"]})
    task_out = whole_routes.task_out(db, task)
    assert task_out["status"] == "failed"
    assert task_out["model_input"]["endpoint"] == "/images/edits"
    assert task_out["model_input"]["prompt"] == fake.edit_calls[0]["prompt"]
    assert task_out["model_input"]["images"][0]["preview_url"] == (
        f"/api/assets/{product.id}/file"
    )


def test_task_rejects_invalid_image_model_and_candidate_limits():
    with pytest.raises(ValidationError):
        WholeImageTaskCreate(prompt="test", image_model="unlisted-model")
    with pytest.raises(ValidationError):
        WholeImageTaskCreate(prompt="test", image_model="gpt-image-2.5-flare")
    with pytest.raises(ValidationError):
        WholeImageTaskCreate(prompt="test", candidate_count=7)
    with pytest.raises(ValidationError):
        WholeImageTaskCreate(prompt="test", candidate_index=6)

    request = WholeImageTaskCreate(prompt="test", candidate_count=6, candidate_index=5)
    assert request.candidate_count == 6
    assert request.candidate_index == 5
    assert WholeImageTaskCreate(prompt="test").candidate_count == 3


def test_task_accepts_six_product_images_and_rejects_seven():
    products = [f"product_{index}" for index in range(6)]
    assert WholeImageTaskCreate(prompt="test", product_asset_ids=products).product_asset_ids == products
    with pytest.raises(ValidationError):
        WholeImageTaskCreate(prompt="test", product_asset_ids=[*products, "product_7"])


@pytest.mark.parametrize("optimized", [False, True])
def test_six_products_reach_generation_and_edit_in_order(settings, store, product_png, logo_png, optimized):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    products = [
        _seed_product(db, settings, store, fake._image((index * 30, 60, 80)), f"product-{index}.png")
        for index in range(6)
    ]
    logo = _seed_product(db, settings, store, logo_png, "merchant-logo.png")
    db.assets.update_one({"_id": logo.id}, {"$set": {"kind": "logo"}})
    style = _seed_style_reference(db, settings, store, product_png, "brand-style.png")
    request = _request(db, settings, store, fake)
    db.brands.update_one(
        {"_id": "seejoy"},
        {"$set": {"logo_asset_ids": [logo.id], "reference_asset_ids": [style.id]}},
    )
    product_ids = [product.id for product in products]
    content_names = [product.original_name for product in products] + ["merchant-logo.png"]
    created = whole_routes.create_task(
        WholeImageTaskCreate(
            prompt="六张素材组合海报", prompt_optimized=optimized,
            product_asset_ids=product_ids, logo_asset_id=logo.id,
            brand_mode="preset", brand_id="seejoy",
        ),
        request, BackgroundTasks(),
    )
    task_id = created["id"]
    whole_service.run_generation(db, settings, store, fake, task_id)
    sent = fake.edit_calls[-1]
    assert sent["reference_names"] == content_names + ([] if optimized else ["brand-style.png"])
    assert "Subject/content reference image(s): 1-6." in sent["prompt"]
    assert "Logo image 7." in sent["prompt"]
    task = db.whole_tasks.find_one({"_id": task_id})
    assert task["status"] == "succeeded"
    assert [image["asset_id"] for image in task["model_input_latest"]["images"]] == (
        product_ids + [logo.id] + ([] if optimized else [style.id])
    )
    version = db.whole_versions.find_one({"task_id": task_id, "version": 1})
    source = db.assets.find_one({"_id": version["output_asset_id"]})
    whole_routes.create_edit(
        task_id,
        WholeImageEditRequest(instruction="只把标题上移", source_version_id=version["_id"]),
        request, BackgroundTasks(),
    )
    whole_service.run_edit(db, settings, store, fake, task_id, "只把标题上移", version["_id"])
    sent = fake.edit_calls[-1]
    assert sent["reference_names"] == content_names + [source["original_name"]]
    assert sent["reference_bytes"] == [
        *(store.get_bytes(product.prepared_path) for product in products),
        store.get_bytes(logo.prepared_path), store.get_bytes(source["path"]),
    ]
    assert "Original content image(s): 1-6." in sent["prompt"]
    assert "Image 7 is the original logo." in sent["prompt"]
    assert "Image 8 is the generated design reference" in sent["prompt"]
    task = db.whole_tasks.find_one({"_id": task_id})
    assert task["edit"]["status"] == "idle"
    assert [image["asset_id"] for image in task["model_input_latest"]["images"]] == (
        product_ids + [logo.id, source["_id"]]
    )


@pytest.mark.parametrize(
    "model",
    ["gemini-3.1-flash-image", "seedream-5-pro-image-to-image"],
)
def test_task_accepts_added_image_models(model):
    assert WholeImageTaskCreate(prompt="test", image_model=model).image_model == model


def test_generation_uses_sunburst_image_model(settings, store):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    created = _create(db, settings, store, fake)

    whole_service.run_generation(db, settings, store, fake, created["id"])

    task = db.whole_tasks.find_one({"_id": created["id"]})
    assert task["input"]["image_model"] == "gpt-image-2.5-sunburst"
    assert task["input"]["candidate_count"] == 3
    assert task["provider"]["model"] == "gpt-image-2.5-sunburst"
    assert fake.generation_calls[0]["model"] == "gpt-image-2.5-sunburst"

    version = db.whole_versions.find_one({"task_id": created["id"], "version": 1})
    whole_routes.create_edit(
        created["id"],
        WholeImageEditRequest(instruction="改背景", source_version_id=version["_id"]),
        _request(db, settings, store, fake),
        BackgroundTasks(),
    )
    whole_service.run_edit(
        db, settings, store, fake, created["id"], "改背景", version["_id"]
    )
    assert fake.edit_calls[-1]["model"] == "gpt-image-2.5-sunburst"


def test_preset_brand_guides_and_reference_images_reach_image_model(settings, store, product_png):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    brand_reference = storage.prepare_upload(
        settings, store, product_png, "brand-poster.png", "brand_reference"
    )
    db.assets.insert_one({
        "_id": brand_reference.id,
        "kind": "brand_reference",
        "original_name": "brand-poster.png",
        "path": brand_reference.path,
        "prepared_path": brand_reference.prepared_path,
        "mime_type": brand_reference.mime_type,
        "width": brand_reference.width,
        "height": brand_reference.height,
        "bytes": brand_reference.bytes,
        "sha256": brand_reference.sha256,
        "created_at": datetime.now(timezone.utc),
    })
    request = _request(db, settings, store, fake)
    db.brands.update_one(
        {"_id": "seejoy"},
        {"$set": {"reference_asset_ids": [brand_reference.id]}},
    )
    created = whole_routes.create_task(
        WholeImageTaskCreate(
            prompt="新品海报",
            ratio="1:1",
            brand_mode="preset",
            brand_id="seejoy",
        ),
        request,
        BackgroundTasks(),
    )

    whole_service.run_generation(db, settings, store, fake, created["id"])

    assert "TEST LAYOUT GUIDE" in fake.edit_calls[0]["prompt"]
    assert "TEST BACKGROUND GUIDE" in fake.edit_calls[0]["prompt"]
    assert "Use attached brand reference images" in fake.edit_calls[0]["prompt"]
    assert "Style-only reference image(s): 1." in fake.edit_calls[0]["prompt"]
    assert fake.edit_calls[0]["reference_bytes"] == [
        store.get_bytes(brand_reference.prepared_path)
    ]


@pytest.mark.parametrize("optimized", [False, True])
def test_selected_logo_purpose_and_palette_reach_generation(
    settings, store, product_png, optimized
):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    product = _seed_product(db, settings, store, product_png, "dish.png")
    logo = _seed_product(db, settings, store, product_png, "merchant-logo.png")
    db.assets.update_one({"_id": logo.id}, {"$set": {"kind": "logo"}})
    request = _request(db, settings, store, fake)
    db.brands.update_one(
        {"_id": "seejoy"},
        {
            "$set": {
                "platform_id": "instagram",
                "placement_id": "story",
                "colors": [
                    {"hex": "#0D4317"},
                    {"hex": "#B12310"},
                ],
                "logo_asset_ids": [logo.id],
            }
        },
    )
    created = whole_routes.create_task(
        WholeImageTaskCreate(
            prompt="Story 新品海报",
            prompt_optimized=optimized,
            product_asset_ids=[product.id],
            brand_id="seejoy",
            brand_mode="preset",
            platform_id="instagram",
            placement_id="story",
            logo_asset_id=logo.id,
        ),
        request,
        BackgroundTasks(),
    )

    whole_service.run_generation(db, settings, store, fake, created["id"])

    task = db.whole_tasks.find_one({"_id": created["id"]})
    sent = fake.edit_calls[0]
    assert task["status"] == "succeeded"
    assert task["input"]["logo_asset_id"] == logo.id
    assert task["brand"]["purpose"] == "Story / Reels 封面"
    assert task["brand"]["colors"][0]["hex"] == "#0D4317"
    assert sent["reference_names"] == ["dish.png", "merchant-logo.png"]
    assert "Logo image 2" in sent["prompt"]
    if not optimized:
        assert "Brand purpose: Story / Reels 封面" in sent["prompt"]
        assert "#0D4317" in sent["prompt"]
    assert task["model_input_latest"]["images"][1]["kind"] == "logo"


def test_task_rejects_logo_outside_selected_brand(settings, store, product_png):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    product = _seed_product(db, settings, store, product_png, "dish.png")
    logo = _seed_product(db, settings, store, product_png, "other-logo.png")
    db.assets.update_one({"_id": logo.id}, {"$set": {"kind": "logo"}})

    with pytest.raises(HTTPException) as exc:
        whole_routes.create_task(
            WholeImageTaskCreate(
                prompt="test",
                product_asset_ids=[product.id],
                brand_id="seejoy",
                brand_mode="preset",
                logo_asset_id=logo.id,
            ),
            _request(db, settings, store, fake),
            BackgroundTasks(),
        )
    assert exc.value.status_code == 422


def test_task_rejects_style_for_a_different_platform(settings, store, product_png):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    product = _seed_product(db, settings, store, product_png, "dish.png")
    request = _request(db, settings, store, fake)
    db.brands.update_one({"_id": "seejoy"}, {"$set": {"platform_id": "instagram"}})

    with pytest.raises(HTTPException) as exc:
        whole_routes.create_task(
            WholeImageTaskCreate(
                prompt="test",
                product_asset_ids=[product.id],
                brand_id="seejoy",
                brand_mode="preset",
                platform_id="xiaohongshu",
            ),
            request,
            BackgroundTasks(),
        )
    assert exc.value.status_code == 422


def test_edit_uses_selected_source_and_creates_branch(settings, store):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    task_id, task = _generate(db, settings, store, fake)
    v1 = db.whole_versions.find_one({"task_id": task_id, "version": 1})
    v1_bytes = store.get_bytes(
        db.assets.find_one({"_id": v1["output_asset_id"]})["path"]
    )
    request = _request(db, settings, store, fake)

    whole_routes.create_edit(
        task_id,
        WholeImageEditRequest(instruction="背景换成橙色", source_version_id=v1["_id"]),
        request,
        BackgroundTasks(),
    )
    whole_service.run_edit(db, settings, store, fake, task_id, "背景换成橙色", v1["_id"])
    v2 = db.whole_versions.find_one({"task_id": task_id, "version": 2})

    assert v2 is not None
    assert v2["parent_version_id"] == v1["_id"]
    assert fake.edit_calls[-1]["reference_bytes"][-1] == v1_bytes
    assert db.whole_tasks.find_one({"_id": task_id})["selected_version_id"] == v2["_id"]

    whole_routes.select_task_version(task_id, v1["_id"], request)
    whole_routes.create_edit(
        task_id,
        WholeImageEditRequest(instruction="标题更有冲击力", source_version_id=v1["_id"]),
        request,
        BackgroundTasks(),
    )
    whole_service.run_edit(db, settings, store, fake, task_id, "标题更有冲击力", v1["_id"])
    versions = list(db.whole_versions.find({"task_id": task_id}).sort("version", 1))

    assert [item["version"] for item in versions] == [1, 2, 3]
    assert versions[2]["parent_version_id"] == v1["_id"]
    assert versions[1]["parent_version_id"] == v1["_id"]

    selected = whole_routes.select_task_version(task_id, v1["_id"], request)
    assert selected["model_input"]["prompt"] == fake.edit_calls[-1]["prompt"]


def test_edit_sends_original_assets_before_current_source(
    settings, store, product_png, logo_png
):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    first = _seed_product(db, settings, store, product_png, "dish.png")
    second = _seed_product(db, settings, store, logo_png, "logo.png")
    created = _create(
        db,
        settings,
        store,
        fake,
        product_asset_ids=[first.id, second.id],
    )
    whole_service.run_generation(db, settings, store, fake, created["id"])
    fake.edit_calls.clear()
    v1 = db.whole_versions.find_one({"task_id": created["id"], "version": 1})
    source_asset = db.assets.find_one({"_id": v1["output_asset_id"]})
    source_bytes = store.get_bytes(source_asset["path"])

    whole_routes.create_edit(
        created["id"],
        WholeImageEditRequest(instruction="把日期改成本周末", source_version_id=v1["_id"]),
        _request(db, settings, store, fake),
        BackgroundTasks(),
    )
    whole_service.run_edit(
        db,
        settings,
        store,
        fake,
        created["id"],
        "把日期改成本周末",
        v1["_id"],
    )

    refs = fake.edit_calls[-1]["reference_bytes"]
    assert len(refs) == 3
    assert refs[0] == store.get_bytes(first.prepared_path)
    assert refs[1] == store.get_bytes(second.prepared_path)
    assert refs[2] == source_bytes
    prompt = fake.edit_calls[-1]["prompt"]
    assert "Original content image(s): 1-2." in prompt
    assert "Image 3 is the generated design reference for style and layout only." in prompt
    assert "Use these originals as the source of subjects and fine details" in prompt
    assert "Do not repaint, retouch, or trace the generated image" in prompt
    assert "do not use it as a pixel source or image-to-image base" in prompt
    assert "Apply only the requested changes" in prompt
    assert "keep all other content, text, composition, and style unchanged" in prompt
    assert prompt.endswith("把日期改成本周末")

    snapshot = db.whole_versions.find_one(
        {"task_id": created["id"], "version": 2}
    )["model_input"]
    assert snapshot["endpoint"] == "/images/edits"
    assert snapshot["prompt"] == prompt
    assert [image["asset_id"] for image in snapshot["images"]] == [
        first.id,
        second.id,
        source_asset["_id"],
    ]
    assert snapshot["images"][-1]["kind"] == "current_design"


@pytest.mark.parametrize("brand_mode", ["preset", "reference"])
@pytest.mark.parametrize("optimized", [False, True])
@pytest.mark.parametrize("remove_style", [False, True])
def test_edit_omits_style_references(
    settings, store, product_png, brand_mode, optimized, remove_style
):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    product = _seed_product(db, settings, store, product_png, "person.png")
    style_reference = _seed_style_reference(
        db, settings, store, product_png, "style-with-person.png"
    )
    request = _request(db, settings, store, fake)
    db.brands.update_one(
        {"_id": "seejoy"},
        {"$set": {"reference_asset_ids": [style_reference.id]}},
    )
    created = whole_routes.create_task(
        WholeImageTaskCreate(
            prompt="把素材中的人物融合进海报",
            prompt_optimized=optimized,
            ratio="1:1",
            product_asset_ids=[product.id],
            brand_id="seejoy" if brand_mode == "preset" else None,
            brand_mode=brand_mode,
            style_reference_asset_ids=[style_reference.id] if brand_mode == "reference" else [],
        ),
        request,
        BackgroundTasks(),
    )
    whole_service.run_generation(db, settings, store, fake, created["id"])
    fake.edit_calls.clear()
    version = db.whole_versions.find_one({"task_id": created["id"], "version": 1})
    source_asset = db.assets.find_one({"_id": version["output_asset_id"]})
    if remove_style:
        db.assets.delete_one({"_id": style_reference.id})

    whole_routes.create_edit(
        created["id"],
        WholeImageEditRequest(
            instruction="调整海报配色", source_version_id=version["_id"]
        ),
        _request(db, settings, store, fake),
        BackgroundTasks(),
    )
    whole_service.run_edit(
        db, settings, store, fake, created["id"], "调整海报配色", version["_id"]
    )

    sent = fake.edit_calls[-1]
    assert sent["reference_names"] == ["person.png", created["id"] + "-v1.png"]
    assert sent["reference_bytes"] == [
        store.get_bytes(product.prepared_path),
        store.get_bytes(source_asset["path"]),
    ]
    assert "Original content image(s): 1." in sent["prompt"]
    assert "Image 2 is the generated design reference for style and layout only." in sent["prompt"]
    assert sent["prompt"].endswith("调整海报配色")
    assert "Style-only reference image(s)" not in sent["prompt"]
    assert "把素材中的人物融合进海报" not in sent["prompt"]
    task = db.whole_tasks.find_one({"_id": created["id"]})
    assert task["edit"]["status"] == "idle"
    snapshot = task["model_input_latest"]
    assert snapshot["prompt"] == sent["prompt"]
    assert [image["asset_id"] for image in snapshot["images"]] == [
        product.id, source_asset["_id"]
    ]
    assert whole_routes.get_task(created["id"], request)["model_input"]["images"] == [
        {**image, "preview_url": f"/api/assets/{image['asset_id']}/file"}
        for image in snapshot["images"]
    ]


def test_edit_keeps_original_logo_without_brand_or_platform_context(settings, store, product_png):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    product = _seed_product(db, settings, store, product_png, "dish.png")
    logo = _seed_product(db, settings, store, product_png, "logo.png")
    db.assets.update_one({"_id": logo.id}, {"$set": {"kind": "logo"}})
    request = _request(db, settings, store, fake)
    db.brands.update_one(
        {"_id": "seejoy"},
        {
            "$set": {
                "platform_id": "instagram",
                "placement_id": "story",
                "colors": [{"hex": "#0D4317"}],
                "logo_asset_ids": [logo.id],
            }
        },
    )
    created = whole_routes.create_task(
        WholeImageTaskCreate(
            prompt="品牌海报",
            product_asset_ids=[product.id],
            brand_id="seejoy",
            brand_mode="preset",
            platform_id="instagram",
            placement_id="story",
            logo_asset_id=logo.id,
        ),
        request,
        BackgroundTasks(),
    )
    whole_service.run_generation(db, settings, store, fake, created["id"])
    fake.edit_calls.clear()
    version = db.whole_versions.find_one({"task_id": created["id"], "version": 1})

    whole_routes.create_edit(
        created["id"],
        WholeImageEditRequest(instruction="把标题上移", source_version_id=version["_id"]),
        request,
        BackgroundTasks(),
    )
    whole_service.run_edit(
        db, settings, store, fake, created["id"], "把标题上移", version["_id"]
    )

    sent = fake.edit_calls[-1]
    assert sent["reference_names"] == ["dish.png", "logo.png", created["id"] + "-v1.png"]
    assert "Image 2 is the original logo." in sent["prompt"]
    assert "Image 3 is the generated design reference for style and layout only." in sent["prompt"]
    assert "Preserve its original shape, wording, proportions, and colors" in sent["prompt"]
    assert sent["prompt"].endswith("把标题上移")
    for redundant_context in (
        "SEEJOY", "test brand", "TEST LAYOUT GUIDE", "TEST BACKGROUND GUIDE",
        "Brand purpose", "#0D4317", "Instagram", "Story", "品牌海报",
    ):
        assert redundant_context not in sent["prompt"]


def test_second_edit_reuses_originals_with_only_current_instruction_and_selected_reference(
    settings, store, product_png
):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    original = _seed_product(db, settings, store, product_png, "original.png")
    task_id = _create(
        db, settings, store, fake, product_asset_ids=[original.id]
    )["id"]
    whole_service.run_generation(db, settings, store, fake, task_id)
    fake.edit_calls.clear()
    v1 = db.whole_versions.find_one({"task_id": task_id, "version": 1})
    request = _request(db, settings, store, fake)

    whole_routes.create_edit(
        task_id,
        WholeImageEditRequest(instruction="把日期改成本周末", source_version_id=v1["_id"]),
        request,
        BackgroundTasks(),
    )
    whole_service.run_edit(
        db, settings, store, fake, task_id, "把日期改成本周末", v1["_id"]
    )
    v2 = db.whole_versions.find_one({"task_id": task_id, "version": 2})

    whole_routes.create_edit(
        task_id,
        WholeImageEditRequest(instruction="标题增加白色描边", source_version_id=v2["_id"]),
        request,
        BackgroundTasks(),
    )
    whole_service.run_edit(
        db, settings, store, fake, task_id, "标题增加白色描边", v2["_id"]
    )

    prompt = fake.edit_calls[-1]["prompt"]
    assert "把日期改成本周末" not in prompt
    assert "标题增加白色描边" in prompt
    assert "prior modifications" not in prompt
    assert "Use these originals as the source of subjects and fine details" in prompt
    assert "Do not repaint, retouch, or trace the generated image" in prompt
    source_asset = db.assets.find_one({"_id": v2["output_asset_id"]})
    original_bytes = store.get_bytes(original.prepared_path)
    assert fake.edit_calls[0]["reference_bytes"][0] == original_bytes
    assert fake.edit_calls[-1]["reference_bytes"] == [
        original_bytes, store.get_bytes(source_asset["path"])
    ]
    assert fake.edit_calls[-1]["reference_names"] == ["original.png", source_asset["original_name"]]
    assert db.whole_tasks.find_one({"_id": task_id})["edit"]["status"] == "idle"


def test_failed_edit_keeps_last_good_version(settings, store):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    task_id, task = _generate(db, settings, store, fake)
    v1 = db.whole_versions.find_one({"task_id": task_id, "version": 1})
    selected_before = task["selected_version_id"]
    count_before = db.whole_versions.count_documents({"task_id": task_id})
    request = _request(db, settings, store, fake)
    whole_routes.create_edit(
        task_id,
        WholeImageEditRequest(instruction="换成完全不同的风格", source_version_id=v1["_id"]),
        request,
        BackgroundTasks(),
    )
    fake.fail_edit = True
    whole_service.run_edit(db, settings, store, fake, task_id, "换成完全不同的风格", v1["_id"])

    after = db.whole_tasks.find_one({"_id": task_id})
    assert after["status"] == "succeeded"
    assert after["selected_version_id"] == selected_before
    assert after["output_asset_id"] == task["output_asset_id"]
    assert after["edit"]["status"] == "failed"
    assert "模拟编辑失败" in after["edit"]["error"]
    assert db.whole_versions.count_documents({"task_id": task_id}) == count_before


def test_select_version_is_not_a_new_version(settings, store):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    task_id, task = _generate(db, settings, store, fake)
    v1 = db.whole_versions.find_one({"task_id": task_id, "version": 1})
    request = _request(db, settings, store, fake)
    whole_routes.create_edit(
        task_id,
        WholeImageEditRequest(instruction="背景换成橙色", source_version_id=v1["_id"]),
        request,
        BackgroundTasks(),
    )
    whole_service.run_edit(db, settings, store, fake, task_id, "背景换成橙色", v1["_id"])
    assert db.whole_versions.count_documents({"task_id": task_id}) == 2

    selected = whole_routes.select_task_version(task_id, v1["_id"], request)
    assert selected["current_version_id"] == v1["_id"]
    assert selected["current_version"] == 1
    assert db.whole_versions.count_documents({"task_id": task_id}) == 2


def test_duplicate_edit_is_rejected_while_running(settings, store):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    task_id, _ = _generate(db, settings, store, fake)
    v1 = db.whole_versions.find_one({"task_id": task_id, "version": 1})
    request = _request(db, settings, store, fake)

    whole_routes.create_edit(
        task_id,
        WholeImageEditRequest(instruction="第一次修改", source_version_id=v1["_id"]),
        request,
        BackgroundTasks(),
    )
    with pytest.raises(HTTPException) as exc:
        whole_routes.create_edit(
            task_id,
            WholeImageEditRequest(instruction="重复修改", source_version_id=v1["_id"]),
            request,
            BackgroundTasks(),
        )
    assert exc.value.status_code == 409


def test_versions_and_download_are_version_aware(settings, store):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    task_id, _ = _generate(db, settings, store, fake)
    v1 = db.whole_versions.find_one({"task_id": task_id, "version": 1})
    request = _request(db, settings, store, fake)

    versions = whole_routes.list_versions(task_id, request)
    assert versions[0]["id"] == v1["_id"]
    assert versions[0]["parent_version_id"] is None
    assert versions[0]["width"] == 640

    response = whole_routes.download_task(
        task_id,
        request,
        version_id=v1["_id"],
    )
    assert response.path
    assert response.filename.endswith("-v1.png")


def test_task_requires_non_blank_prompt():
    with pytest.raises(ValidationError):
        WholeImageTaskCreate(prompt="   ", ratio="1:1")


def test_task_prompt_accepts_up_to_5000_characters():
    prompt = "长" * 5000
    assert WholeImageTaskCreate(prompt=prompt).prompt == prompt

    with pytest.raises(ValidationError):
        WholeImageTaskCreate(prompt=prompt + "长")



def test_platform_whole_task_snapshots_rules_and_allows_edit(settings, store):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    request = _request(db, settings, store, fake)
    created = whole_routes.create_task(
        WholeImageTaskCreate(
            prompt="新品海报",
            platform_id="xiaohongshu",
            placement_id="note",
        ),
        request,
        BackgroundTasks(),
    )
    assert created["canvas"] == {"width": 1080, "height": 1440}
    assert created["platform"]["id"] == "xiaohongshu"
    assert created["input"]["ratio"] == "3:4"

    whole_service.run_generation(db, settings, store, fake, created["id"])
    version = db.whole_versions.find_one({"task_id": created["id"], "version": 1})
    pending = whole_routes.create_edit(
        created["id"],
        WholeImageEditRequest(
            instruction="把底部标题向上移动",
            source_version_id=version["_id"],
        ),
        request,
        BackgroundTasks(),
    )
    assert pending["status"] == "succeeded"
    assert pending["edit"]["status"] == "running"


def test_platform_generation_prompt_has_no_contact_fields(settings, store):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    request = _request(db, settings, store, fake)
    created = whole_routes.create_task(
        WholeImageTaskCreate(
            prompt="夏日新品海报",
            platform_id="xiaohongshu",
            placement_id="note",
        ),
        request,
        BackgroundTasks(),
    )
    whole_service.run_generation(db, settings, store, fake, created["id"])
    task = db.whole_tasks.find_one({"_id": created["id"]})

    assert task["status"] == "succeeded"
    assert "1080×1440" in fake.generation_calls[0]["prompt"]
    assert "禁止出现地址、电话号码或二维码" in fake.generation_calls[0]["prompt"]
    assert "商家地址和联系电话" not in fake.generation_calls[0]["prompt"]
    assert db.whole_versions.count_documents({"task_id": created["id"]}) == 1


def test_edit_history_is_isolated_to_selected_branch(settings, store):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    task_id, _ = _generate(db, settings, store, fake)
    v1 = db.whole_versions.find_one({"task_id": task_id, "version": 1})
    request = _request(db, settings, store, fake)

    whole_routes.create_edit(
        task_id,
        WholeImageEditRequest(instruction="分支A：把背景改成橙色", source_version_id=v1["_id"]),
        request,
        BackgroundTasks(),
    )
    whole_service.run_edit(
        db, settings, store, fake, task_id, "分支A：把背景改成橙色", v1["_id"]
    )

    whole_routes.create_edit(
        task_id,
        WholeImageEditRequest(instruction="分支B：把标题改大", source_version_id=v1["_id"]),
        request,
        BackgroundTasks(),
    )
    whole_service.run_edit(
        db, settings, store, fake, task_id, "分支B：把标题改大", v1["_id"]
    )

    prompt = fake.edit_calls[-1]["prompt"]
    assert "分支B：把标题改大" in prompt
    assert "分支A：把背景改成橙色" not in prompt
