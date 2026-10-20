from __future__ import annotations

import io
from datetime import datetime, timezone

import mongomock
from PIL import Image, ImageDraw

from app.services import design as design_utils
from app.services import pipeline, storage
from app.services.core_ai import CoreAIError


class FakeCoreAI:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.submitted = 0
        self.last_prompt = ""
        self.last_reference_count = 0
        self.last_size = None

    def edit_image(self, prompt, references, *, size, model=None, quality=None, mask=None):
        self.submitted += 1
        self.last_prompt = prompt
        self.last_reference_count = len(references)
        self.last_size = size
        if self.fail:
            raise CoreAIError("generation_failed", "simulated failure")
        buffer = io.BytesIO()
        Image.new("RGB", (700, 700), (80, 50, 40)).save(buffer, format="PNG")
        return buffer.getvalue()

    def generate_image(self, prompt, *, size, model=None, quality=None):
        self.submitted += 1
        self.last_prompt = prompt
        self.last_size = size
        if self.fail:
            raise CoreAIError("generation_failed", "simulated failure")
        buffer = io.BytesIO()
        Image.new("RGB", (700, 700), (80, 50, 40)).save(buffer, format="PNG")
        return buffer.getvalue()

    def chat_json(self, messages, *, model=None, timeout_seconds=None):
        return {"status": "pass", "violations": []}


def seed_assets(db, settings, store, product_png, logo_png):
    now = datetime.now(timezone.utc)
    product = storage.prepare_upload(settings, store, product_png, "dish.png", "product")
    logo = storage.prepare_upload(settings, store, logo_png, "logo.png", "logo")
    for prepared in (product, logo):
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
                "created_at": now,
            }
        )
    return product, logo


def make_task(db, product, logo):
    now = datetime.now(timezone.utc)
    task_id = "task_test"
    db.tasks.insert_one(
        {
            "_id": task_id,
            "status": "queued",
            "stage": "pending",
            "progress": 0,
            "input": {
                "product_asset_ids": [product.id],
                "logo_asset_id": logo.id,
                "prompt": "",
                "ratio": "1:1",
            },
            "canvas": {"width": 1024, "height": 1024},
            "provider": {
                "name": "core-ai",
                "model": "gpt-image-2.5-flare",
                "task_id": None,
                "status": None,
                "progress": 0,
            },
            "background_asset_id": None,
            "output_asset_id": None,
            "design": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
    )
    return task_id


def test_pipeline_success(settings, store, product_png, logo_png):
    db = mongomock.MongoClient().db
    product, logo = seed_assets(db, settings, store, product_png, logo_png)
    task_id = make_task(db, product, logo)
    fake = FakeCoreAI()
    pipeline.run_task(db, settings, store, fake, task_id)
    task = db.tasks.find_one({"_id": task_id})
    assert task["status"] == "succeeded"
    assert task["stage"] == "done"
    assert task["progress"] == 100
    assert task["output_asset_id"]
    assert db.assets.find_one({"_id": task["output_asset_id"]})["kind"] == "output"
    assert fake.submitted == 1
    assert fake.last_reference_count == 1
    assert fake.last_size == "1024x1024"
    assert task["current_version"] == 1
    assert task["design"]["layers"][0]["type"] == "background"
    assert any(layer["type"] == "text" for layer in task["design"]["layers"])
    assert not any(layer.get("role") == "logo" for layer in task["design"]["layers"])
    versions = list(db.versions.find({"task_id": task_id}))
    assert len(versions) == 1
    assert versions[0]["version"] == 1


def test_pipeline_failure_marks_task(settings, store, product_png, logo_png):
    db = mongomock.MongoClient().db
    product, logo = seed_assets(db, settings, store, product_png, logo_png)
    task_id = make_task(db, product, logo)
    pipeline.run_task(db, settings, store, FakeCoreAI(fail=True), task_id)
    task = db.tasks.find_one({"_id": task_id})
    assert task["status"] == "failed"
    assert task["error"]["code"] == "generation_failed"


def test_can_resume_restarts_interrupted_sync_task(settings, store, product_png, logo_png):
    db = mongomock.MongoClient().db
    product, logo = seed_assets(db, settings, store, product_png, logo_png)
    task_id = make_task(db, product, logo)
    db.tasks.update_one({"_id": task_id}, {"$set": {"status": "interrupted"}})
    allowed, _ = pipeline.can_resume(db, task_id)
    assert allowed is True


def test_mark_interrupted_fails_running_edits():
    db = mongomock.MongoClient().db
    db.tasks.insert_one(
        {
            "_id": "task_running",
            "status": "running",
            "stage": "generating",
            "edit": {"status": "running"},
        }
    )
    pipeline.mark_interrupted(db)
    task = db.tasks.find_one({"_id": "task_running"})
    assert task["status"] == "interrupted"
    assert task["edit"]["status"] == "failed"
    assert "重启" in task["edit"]["error"]


def test_pipeline_fuses_multiple_products(settings, store, product_png, logo_png):
    db = mongomock.MongoClient().db
    product, logo = seed_assets(db, settings, store, product_png, logo_png)
    second = storage.prepare_upload(
        settings, store, product_png, "dish2.png", "product"
    )
    db.assets.insert_one(
        {
            "_id": second.id,
            "kind": "product",
            "original_name": second.original_name,
            "path": second.path,
            "prepared_path": second.prepared_path,
            "mime_type": second.mime_type,
            "width": second.width,
            "height": second.height,
            "bytes": second.bytes,
            "sha256": second.sha256,
            "created_at": datetime.now(timezone.utc),
        }
    )
    task_id = make_task(db, product, logo)
    db.tasks.update_one(
        {"_id": task_id},
        {"$set": {"input.product_asset_ids": [product.id, second.id]}},
    )
    fake = FakeCoreAI()
    pipeline.run_task(db, settings, store, fake, task_id)
    task = db.tasks.find_one({"_id": task_id})
    assert task["status"] == "succeeded"
    assert fake.last_reference_count == 2


def test_pipeline_uses_brand_snapshot_for_generation_prompt(
    settings, store, product_png, logo_png
):
    db = mongomock.MongoClient().db
    product, logo = seed_assets(db, settings, store, product_png, logo_png)
    task_id = make_task(db, product, logo)
    db.tasks.update_one(
        {"_id": task_id},
        {
            "$set": {
                "input.brand_id": "custom",
                "brand": {
                    "id": "custom",
                    "name": "Custom Brand",
                    "short_name": "Custom",
                    "summary": "custom",
                    "layout_guide": "CUSTOM LAYOUT GUIDE",
                    "background_guide": "CUSTOM BACKGROUND GUIDE",
                    "logo_keywords": ["custom"],
                },
            }
        },
    )
    fake = FakeCoreAI()
    pipeline.run_task(db, settings, store, fake, task_id)
    task = db.tasks.find_one({"_id": task_id})
    assert task["status"] == "succeeded"
    assert "CUSTOM BACKGROUND GUIDE" in fake.last_prompt
    assert "不要出现任何文字" in fake.last_prompt


def test_build_prompt_uses_user_prompt():
    text = pipeline.build_prompt("深色石板背景，顶部聚光灯", has_logo=True)
    assert "深色石板背景" in text
    assert "右上角" in text
    assert "不要出现任何文字" in text
    assert "Logo" in text


def test_build_prompt_defaults_without_logo():
    text = pipeline.build_prompt("", has_logo=False)
    assert "高端火锅" in text or "中餐" in text
    assert "右上角" not in text


def test_build_prompt_uses_brand_background_guide():
    from app.config import PROJECT_DIR
    from app.services import brand_style

    brand = brand_style.load_brand_skill(PROJECT_DIR / ".agents" / "skills" / "seejoy-brand-poster")
    text = pipeline.build_prompt("夜宵自助", has_logo=False, brand=brand)
    assert "真实餐饮摄影质感" in text
    assert "深森林绿" in text


def test_build_prompt_multi_image_and_ratio():
    text = pipeline.build_prompt(
        "生成一张 IG 海报", has_logo=True, product_count=3, ratio="9:16"
    )
    assert "3 张" in text
    assert "融合成同一张商业海报底图" in text
    assert "9:16" in text


def test_exact_logo_overlay_preserves_alpha_and_position():
    background = Image.new("RGB", (220, 220), (10, 20, 30)).convert("RGBA")
    logo = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    for x in range(20, 80):
        for y in range(20, 80):
            logo.putpixel((x, y), (220, 20, 20, 255))
    result = pipeline.overlay_exact_logo(
        background,
        logo,
        box={"x": 60, "y": 60},
        size={"width": 100, "height": 100},
    )
    assert result.getpixel((62, 62))[:3] == (10, 20, 30)
    assert result.getpixel((110, 110))[:3] == (220, 20, 20)


def test_layout_items_preserve_copy_and_rotation():
    headline = design_utils.make_text_layer(
        layer_id="text_headline",
        name="主标题",
        role="headline",
        content="夜宵时间到",
        box={"x": 30, "y": 130, "width": 530, "height": 100},
        rotation=14,
    )
    price = design_utils.make_text_layer(
        layer_id="text_price",
        name="价格",
        role="price",
        content="$56.99/位",
        box={"x": 33, "y": 376, "width": 315, "height": 62},
    )
    logo = design_utils.make_logo_layer(
        layer_id="logo_brand",
        asset_id="asset_logo",
        box={"x": 916, "y": 24, "width": 82, "height": 82},
    )
    design = design_utils.make_design(
        canvas={"width": 1080, "height": 1350},
        clean_asset_id="asset_clean",
        render_asset_id="asset_render",
        background_prompt="夜晚餐厅",
        logo_integrated=True,
        text_layers=[headline, price],
        logo_layer=logo,
    )
    items = pipeline._layout_items_from_design(design)
    assert [item["content"] for item in items] == ["夜宵时间到", "$56.99/位"]
    assert items[0]["rotation"] == 14
    assert design_utils.logo_layer(design)["render_hint"] == "baked"
class GhostLogoCoreAI:
    def edit_image(self, prompt, references, *, size, model=None, quality=None, mask=None):
        source_ref = getattr(references[0], "path", references[0])
        with Image.open(source_ref) as source:
            crop = source.convert("RGB").copy()
        draw = ImageDraw.Draw(crop)
        # Simulate a model-rendered logo inside the slot plus a duplicate that
        # spills left of the reserved box.
        draw.ellipse((45, 40, 59, 58), fill=(220, 20, 20))
        draw.ellipse((75, 45, 85, 55), fill=(220, 20, 20))
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG")
        return buffer.getvalue()


def test_logo_integration_erases_duplicate_mark_outside_reserved_box(
    settings, store, logo_png
):
    db = mongomock.MongoClient().db
    local_settings = settings.model_copy(
        update={"logo_integration_enabled": True, "logo_exact_overlay": True}
    )
    background = Image.new("RGB", (400, 400), (10, 20, 30))
    background_id, _ = pipeline._save_image_asset(
        db,
        local_settings,
        store,
        task_id="task_ghost",
        name="clean.png",
        data=storage.encode_png(background),
        kind="background",
    )
    prepared = storage.prepare_upload(
        local_settings, store, logo_png, "logo.png", "logo"
    )
    db.assets.insert_one(
        {
            "_id": prepared.id,
            "kind": "logo",
            "original_name": prepared.original_name,
            "path": prepared.path,
            "prepared_path": prepared.prepared_path,
            "mime_type": prepared.mime_type,
            "width": prepared.width,
            "height": prepared.height,
            "bytes": prepared.bytes,
            "sha256": prepared.sha256,
        }
    )
    logo_layer = design_utils.make_logo_layer(
        layer_id="logo_brand",
        asset_id=prepared.id,
        box={"x": 280, "y": 30, "width": 60, "height": 60},
    )
    output_id = pipeline._integrate_logo(
        db,
        local_settings,
        store,
        GhostLogoCoreAI(),
        task_id="task_ghost",
        version=1,
        background_asset_id=background_id,
        logo_asset=db.assets.find_one({"_id": prepared.id}),
        logo_layer=logo_layer,
        canvas={"width": 400, "height": 400},
    )
    assert output_id
    output = storage.open_image(store, db.assets.find_one({"_id": output_id})["path"])
    assert output.convert("RGB").getpixel((260, 50)) == (10, 20, 30)


def test_platform_prompt_reaches_background_generation(settings, store, product_png, logo_png):
    db = mongomock.MongoClient().db
    product, logo = seed_assets(db, settings, store, product_png, logo_png)
    task_id = make_task(db, product, logo)
    db.tasks.update_one(
        {"_id": task_id},
        {
            "$set": {
                "platform": {
                    "id": "xiaohongshu",
                    "name": "小红书",
                    "contact_policy": "forbidden",
                    "default_placement_id": "note",
                    "placements": [
                        {
                            "id": "note",
                            "name": "图文笔记 / 封面",
                            "ratio": "3:4",
                            "width": 1080,
                            "height": 1440,
                            "safe_area": {"top": 0.05, "right": 0.05, "bottom": 0.05, "left": 0.05},
                        }
                    ],
                },
                "placement": {
                    "id": "note",
                    "name": "图文笔记 / 封面",
                    "ratio": "3:4",
                    "width": 1080,
                    "height": 1440,
                    "safe_area": {"top": 0.05, "right": 0.05, "bottom": 0.05, "left": 0.05},
                },
                "canvas": {"width": 1080, "height": 1440},
            }
        },
    )
    fake = FakeCoreAI()
    pipeline.run_task(db, settings, store, fake, task_id)
    assert "1080×1440" in fake.last_prompt
    assert "不得出现地址" in fake.last_prompt


def test_logo_kind_asset_can_be_used_as_an_ordinary_reference(settings, store, product_png, logo_png):
    db = mongomock.MongoClient().db
    _, logo = seed_assets(db, settings, store, product_png, logo_png)
    loaded = pipeline._load_products(db, {"product_asset_ids": [logo.id]})
    assert [item["_id"] for item in loaded] == [logo.id]


def test_task_without_brand_uses_free_creation_brand(settings, store, product_png, logo_png):
    db = mongomock.MongoClient().db
    product, logo = seed_assets(db, settings, store, product_png, logo_png)
    task_id = make_task(db, product, logo)
    task = db.tasks.find_one({"_id": task_id})
    assert pipeline.brand_style.brand_for_task(task, db, store) is None


def test_reference_mode_passes_style_images_to_generation(settings, store, product_png, logo_png):
    db = mongomock.MongoClient().db
    product, logo = seed_assets(db, settings, store, product_png, logo_png)
    style = storage.prepare_upload(settings, store, product_png, "style.png", "brand_reference")
    db.assets.insert_one(
        {
            "_id": style.id,
            "kind": style.kind,
            "original_name": style.original_name,
            "path": style.path,
            "prepared_path": style.prepared_path,
            "mime_type": style.mime_type,
            "width": style.width,
            "height": style.height,
            "bytes": style.bytes,
            "sha256": style.sha256,
            "created_at": datetime.now(timezone.utc),
        }
    )
    task_id = make_task(db, product, logo)
    db.tasks.update_one(
        {"_id": task_id},
        {
            "$set": {
                "input": {
                    "product_asset_ids": [product.id],
                    "brand_id": None,
                    "brand_mode": "reference",
                    "style_reference_asset_ids": [style.id],
                    "prompt": "",
                    "ratio": "1:1",
                    "generation_mode": "classic",
                }
            }
        },
    )
    fake = FakeCoreAI()
    pipeline.run_task(db, settings, store, fake, task_id)
    assert fake.last_reference_count == 2
