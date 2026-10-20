from __future__ import annotations

import io
from datetime import datetime, timezone

import mongomock
from PIL import Image

from app.services import design as design_utils
from app.services import pipeline, storage
from app.services.toapis import ToAPIsError


class FakeToAPIs:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.submitted = 0

    def upload_reference(self, path):
        return "https://files.toapis.xyz/uploads/reference.png"

    def submit_generation(self, prompt, reference_urls, ratio=None, resolution=None):
        self.submitted += 1
        self.last_prompt = prompt
        self.last_reference_count = len(reference_urls)
        self.last_ratio = ratio
        self.last_resolution = resolution
        return {"id": "provider_task_1", "status": "queued"}

    def wait_for_generation(self, task_id, on_update=None):
        if on_update:
            on_update({"status": "completed", "progress": 100})
        if self.fail:
            raise ToAPIsError("generation_failed", "simulated failure")
        return "https://files.toapis.xyz/images/final.png"

    def download_result(self, url):
        buffer = io.BytesIO()
        Image.new("RGB", (700, 700), (80, 50, 40)).save(buffer, format="PNG")
        return buffer.getvalue()


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
                "name": "toapis",
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
    fake = FakeToAPIs()
    pipeline.run_task(db, settings, store, fake, task_id)
    task = db.tasks.find_one({"_id": task_id})
    assert task["status"] == "succeeded"
    assert task["stage"] == "done"
    assert task["progress"] == 100
    assert task["output_asset_id"]
    assert db.assets.find_one({"_id": task["output_asset_id"]})["kind"] == "output"
    assert fake.submitted == 1
    assert fake.last_reference_count == 1
    assert fake.last_ratio == "1:1"
    assert task["current_version"] == 1
    assert task["design"]["layers"][0]["type"] == "background"
    assert any(layer["type"] == "text" for layer in task["design"]["layers"])
    assert any(layer["type"] == "logo" for layer in task["design"]["layers"])
    versions = list(db.versions.find({"task_id": task_id}))
    assert len(versions) == 1
    assert versions[0]["version"] == 1


def test_pipeline_failure_marks_task(settings, store, product_png, logo_png):
    db = mongomock.MongoClient().db
    product, logo = seed_assets(db, settings, store, product_png, logo_png)
    task_id = make_task(db, product, logo)
    pipeline.run_task(db, settings, store, FakeToAPIs(fail=True), task_id)
    task = db.tasks.find_one({"_id": task_id})
    assert task["status"] == "failed"
    assert task["error"]["code"] == "generation_failed"


def test_can_resume_requires_provider_id(settings, store, product_png, logo_png):
    db = mongomock.MongoClient().db
    product, logo = seed_assets(db, settings, store, product_png, logo_png)
    task_id = make_task(db, product, logo)
    db.tasks.update_one({"_id": task_id}, {"$set": {"status": "interrupted"}})
    allowed, _ = pipeline.can_resume(db, task_id)
    assert allowed is False
    db.tasks.update_one(
        {"_id": task_id}, {"$set": {"provider.task_id": "provider_task_1"}}
    )
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
    fake = FakeToAPIs()
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
                    "version": 1,
                    "palette": {"primary": "#112233"},
                    "fonts": {"display": "brush", "body": "sans", "price": "sans"},
                    "layout_guide": "CUSTOM LAYOUT GUIDE",
                    "background_guide": "CUSTOM BACKGROUND GUIDE",
                    "logo_keywords": ["custom"],
                },
            }
        },
    )
    fake = FakeToAPIs()
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

    brand = brand_style.get_brand(PROJECT_DIR / ".agents" / "skills", "seejoy")
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


def test_relayout_preserves_copy_background_and_logo():
    headline = design_utils.make_text_layer(
        layer_id="text_headline",
        name="主标题",
        role="headline",
        content="夜宵时间到",
        box={"x": 30, "y": 130, "width": 530, "height": 100},
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
    result = pipeline._relayout_design(design, has_logo=True)
    assert result["background"] == design["background"]
    assert design_utils.logo_layer(result) == design_utils.logo_layer(design)
    assert design_utils.copy_from_design(result) == design_utils.copy_from_design(design)
    new_price = design_utils.find_layer_by_role(result, "price")
    assert new_price["position"]["y"] > 1350 * 0.55
    assert result["layers"][1].get("rotation") is not None
