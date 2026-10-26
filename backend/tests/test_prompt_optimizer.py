from __future__ import annotations

from datetime import datetime, timezone

import mongomock
import pytest
from pydantic import ValidationError

from app.config import PROJECT_DIR, Settings
from app.schemas import PromptOptimizeRequest
from app.services import prompt_optimizer, storage
from app.services.core_ai import CoreAIError
from app.services.object_store import CosObjectStore


@pytest.fixture
def settings(tmp_path):
    return Settings(
        storage_dir=str(tmp_path / "storage"), sample_dir=str(PROJECT_DIR / "seejoy素材" / "素材"),
        core_ai_api_key="test-key", cos_secret_id="test", cos_secret_key="test",
        cos_bucket="test-bucket", cos_region="ap-guangzhou",
    )


class FakeCosStore(CosObjectStore):
    def __init__(self):
        self.objects = {}

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = data

    def get_bytes(self, key):
        return self.objects[key]

    def local_path(self, key):
        return None

    def presign(self, key, expires, download_name=None):
        assert key in self.objects
        assert expires >= 190
        return f"https://test-bucket.cos.ap-guangzhou.myqcloud.com/{key}?signed=1"


@pytest.fixture
def store():
    return FakeCosStore()


class FakeCoreAI:
    def __init__(self, response: dict | None = None) -> None:
        self.response = response if response is not None else {"prompt": "优化后的中文海报提示词"}
        self.calls: list[dict] = []

    def run_agent(self, agent_id, input_text, *, timeout_seconds):
        import json

        self.calls.append({"agent_id": agent_id, "input": input_text, "timeout_seconds": timeout_seconds})
        return json.dumps(self.response, ensure_ascii=False)


def _seed_asset(db, settings, store, name: str, kind: str, color: tuple[int, int, int]):
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (240, 320), color).save(buffer, format="PNG")
    prepared = storage.prepare_upload(settings, store, buffer.getvalue(), name, kind)
    now = datetime.now(timezone.utc)
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
    return prepared.id


def _seed_brand(db, reference_ids: list[str] | None = None) -> None:
    now = datetime.now(timezone.utc)
    db.brands.insert_one(
        {
            "_id": "seejoy",
            "name": "SEEJOY 喜聚",
            "short_name": "喜聚",
            "summary": "深森林绿、品牌红和奶油白的真实餐饮拼贴风格。",
            "is_default": True,
            "layout_guide": "使用毛笔主标题、手写批注和撕纸价签。",
            "background_guide": "真实餐饮摄影，暖色灯光，深绿与红色点缀。",
            "logo_keywords": ["seejoy"],
            "cover_asset_id": None,
            "reference_asset_ids": list(reference_ids or []),
            "created_at": now,
            "updated_at": now,
        }
    )


@pytest.fixture
def optimizer_db(settings, store):
    db = mongomock.MongoClient().db
    products = [
        _seed_asset(db, settings, store, "dish.png", "product", (180, 70, 40)),
        _seed_asset(db, settings, store, "invite.jpg", "product", (235, 220, 170)),
    ]
    brand_reference = _seed_asset(
        db, settings, store, "brand-reference.jpg", "brand_reference", (20, 70, 30)
    )
    _seed_brand(db, [brand_reference])
    return db, products, brand_reference


def _optimize(db, settings, store, client, **overrides):
    payload = {
        "prompt": "",
        "brand_mode": "preset",
        "brand_id": "seejoy",
        "style_reference_asset_ids": [],
        "product_asset_ids": [],
        "platform_id": None,
        "placement_id": None,
    }
    revision_guidance = overrides.pop("revision_guidance", None)
    payload.update(overrides)
    return prompt_optimizer.optimize_prompt(
        db,
        settings,
        store,
        client,
        request=PromptOptimizeRequest(**payload),
        revision_guidance=revision_guidance,
    )


@pytest.mark.parametrize("brand_mode", ["none", "preset", "reference"])
def test_optimizer_common_rules_all_modes_and_structured_guide(settings, store, optimizer_db, brand_mode):
    from app.services import brand_rules

    db, products, reference_id = optimizer_db
    db.brand_rules.insert_one({"_id": "default", **brand_rules.DEFAULT_RULES, "copy_accuracy": "COMMON_COPY_RULE"})
    db.brands.update_one({"_id": "seejoy"}, {"$set": {
        "schema_version": 2,
        "style_fields": {"palette_notes": "绿色", "typography": "V2_FONT_RULE", "subject_style": "实拍", "background": "纸感"},
        "layout_guide": "LEGACY_MUST_NOT_APPEAR", "background_guide": "OLD_BACKGROUND",
    }})
    client = FakeCoreAI()
    _optimize(db, settings, store, client, brand_mode=brand_mode, brand_id="seejoy" if brand_mode == "preset" else None,
              product_asset_ids=products, style_reference_asset_ids=[reference_id] if brand_mode == "reference" else [])
    text = client.calls[0]["input"]
    assert "COMMON_COPY_RULE" in text
    assert "LEGACY_MUST_NOT_APPEAR" not in text
    if brand_mode == "preset":
        assert text.count("V2_FONT_RULE") == 1


def test_optimizer_inherits_brand_platform_when_not_explicit(settings, store, optimizer_db):
    db, products, _ = optimizer_db
    db.brands.update_one({"_id": "seejoy"}, {"$set": {"platform_id": "xiaohongshu", "placement_id": "note"}})
    client = FakeCoreAI()
    _optimize(db, settings, store, client, product_asset_ids=products)
    text = client.calls[0]["input"]
    assert "小红书" in text
    assert "电话号码" in text


def test_optimizer_passes_all_six_products_and_rejects_seven(settings, store):
    import json

    db = mongomock.MongoClient().db
    products = [
        _seed_asset(db, settings, store, f"product-{index}.png", "product", (index * 30, 60, 80))
        for index in range(6)
    ]
    client = FakeCoreAI()
    _optimize(db, settings, store, client, brand_mode="none", brand_id=None, product_asset_ids=products)
    manifest = json.loads(client.calls[0]["input"].split("图片清单（JSON）：\n", 1)[1])
    assert [image["label"] for image in manifest["content_images"]] == [
        f"素材图 {index}" for index in range(1, 7)
    ]
    assert [image["url"] for image in manifest["content_images"]] == [
        store.presign(db.assets.find_one({"_id": asset_id})["prepared_path"], 3600)
        for asset_id in products
    ]
    with pytest.raises(ValidationError):
        PromptOptimizeRequest(product_asset_ids=[*products, "seventh"])


def test_blank_prompt_uses_products_brand_reference_and_xiaohongshu_rules(
    settings, store, optimizer_db
):
    db, products, _ = optimizer_db
    client = FakeCoreAI()

    result = _optimize(
        db,
        settings,
        store,
        client,
        product_asset_ids=products,
        platform_id="xiaohongshu",
        placement_id="note",
    )

    assert result == "优化后的中文海报提示词"
    assert client.calls[0]["agent_id"] == settings.core_ai_prompt_agent_id
    text = client.calls[0]["input"]
    assert text.count("https://test-bucket.cos.") == 3
    assert "data:image" not in text
    assert "当前提示词为空" in text
    assert "品牌：SEEJOY 喜聚" in text
    assert "使用毛笔主标题" in text
    assert "小红书" in text
    assert "商家地址和联系电话" not in text
    assert "素材图 1" in text
    assert "品牌参考图 1" in text
    assert "额外贴纸或涂鸦最多一处" not in text


def test_unified_generation_guide_is_injected_once(settings, store, optimizer_db):
    db, products, _ = optimizer_db
    guide = "统一生成规范：暖调复古、实拍保真、少文字。"
    db.brands.update_one(
        {"_id": "seejoy"},
        {"$set": {"layout_guide": guide, "background_guide": guide}},
    )
    client = FakeCoreAI()

    _optimize(db, settings, store, client, product_asset_ids=products)

    text = client.calls[0]["input"]
    assert text.count(guide) == 1
    assert "品牌生成规范：" in text
    assert "品牌摄影与背景规范" not in text

def test_unselected_platform_uses_freeform_guidance(settings, store, optimizer_db):
    db, products, _ = optimizer_db
    client = FakeCoreAI()

    _optimize(db, settings, store, client, product_asset_ids=products)

    text = client.calls[0]["input"]
    assert "发布平台未指定" in text
    assert "自主选择最合适的构图" in text


def test_reference_mode_uses_uploaded_style_images_without_preset_guide(
    settings, store, optimizer_db
):
    db, products, brand_reference = optimizer_db
    client = FakeCoreAI()

    _optimize(
        db,
        settings,
        store,
        client,
        prompt="根据素材图生成海报",
        brand_mode="reference",
        brand_id=None,
        style_reference_asset_ids=[brand_reference],
        product_asset_ids=products[:1],
    )

    text = client.calls[0]["input"]
    assert "风格参考图 1" in text
    assert "只参考配色、构图、字体气质和氛围" in text
    assert "使用毛笔主标题" not in text
    assert "整图直接生成" in text


def test_image_manifest_keeps_content_sources_separate_from_style_sources(
    settings, store, optimizer_db
):
    import json

    db, products, brand_reference = optimizer_db
    client = FakeCoreAI()
    _optimize(db, settings, store, client, product_asset_ids=products)

    manifest = json.loads(client.calls[0]["input"].split("图片清单（JSON）：\n", 1)[1])
    assert [image["label"] for image in manifest["content_images"]] == ["素材图 1", "素材图 2"]
    assert [image["label"] for image in manifest["style_images"]] == ["品牌参考图 1"]
    assert products[1] in manifest["content_images"][1]["url"]
    assert brand_reference in manifest["style_images"][0]["url"]


def test_optimizer_includes_generation_metadata_and_selected_logo(
    settings, store, optimizer_db
):
    db, products, _ = optimizer_db
    logo = _seed_asset(db, settings, store, "merchant-logo.png", "logo", (10, 20, 30))
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
                "logo_asset_ids": [logo],
            }
        },
    )
    client = FakeCoreAI()

    _optimize(
        db,
        settings,
        store,
        client,
        product_asset_ids=products[:1],
        platform_id="instagram",
        placement_id="story",
        logo_asset_id=logo,
    )

    text = client.calls[0]["input"]
    manifest = __import__("json").loads(text.split("图片清单（JSON）：\n", 1)[1])
    assert "品牌用途：Story / Reels 封面" in text
    assert "#0D4317" in text
    assert "#B12310" in text
    assert "商家 Logo" in text
    assert logo in manifest["logo_images"][0]["url"]
    assert manifest["logo_images"][0]["url"].endswith("?signed=1")


def test_optimizer_rejects_brand_style_for_another_platform(settings, store, optimizer_db):
    db, products, _ = optimizer_db
    db.brands.update_one(
        {"_id": "seejoy"},
        {"$set": {"platform_id": "instagram", "placement_id": "story"}},
    )

    with pytest.raises(prompt_optimizer.PromptOptimizerError) as exc:
        _optimize(
            db,
            settings,
            store,
            FakeCoreAI(),
            product_asset_ids=products[:1],
            platform_id="xiaohongshu",
            placement_id="note",
        )

    assert exc.value.code == "brand_platform_mismatch"


@pytest.mark.parametrize("prompt", [
    "Primary request: a ceramic mug\nStyle/medium: product photography",
    'Use case: ads-marketing\nSubject: visiting chef\nText (verbatim): "欢迎到店"',
    "用真实人物照片制作一张活动海报，保持人物身份与动作。",
])
def test_optimizer_accepts_flexible_prompt_without_custom_fact_schema(settings, store, optimizer_db, prompt):
    db, products, _ = optimizer_db
    client = FakeCoreAI({"prompt": prompt})

    assert _optimize(db, settings, store, client, product_asset_ids=products) == prompt
    sent = client.calls[0]["input"]
    assert "required_facts" not in sent
    assert "七段" not in sent
    assert "Text 分组" not in sent


def test_optimizer_forwards_revision_guidance_for_benchmark(settings, store, optimizer_db):
    db, products, _ = optimizer_db
    client = FakeCoreAI()

    _optimize(
        db,
        settings,
        store,
        client,
        prompt="上一轮海报提示词",
        product_asset_ids=products,
        revision_guidance="提高毛笔标题占比，修复日期小字乱码。",
    )

    text = client.calls[0]["input"]
    assert "提高毛笔标题占比" in text
    assert "本次修改要求" in text


def test_rejects_invalid_or_repeated_assets(settings, store, optimizer_db):
    db, products, _ = optimizer_db
    client = FakeCoreAI()

    with pytest.raises(prompt_optimizer.PromptOptimizerError) as exc:
        _optimize(db, settings, store, client, product_asset_ids=[products[0], products[0]])
    assert exc.value.status_code == 422
    assert "不能重复" in exc.value.message

    with pytest.raises(prompt_optimizer.PromptOptimizerError):
        _optimize(db, settings, store, client, product_asset_ids=["missing"])
    assert not client.calls


def test_optimizer_requires_cos_for_agent_images(settings, store, optimizer_db):
    db, products, _ = optimizer_db
    client = FakeCoreAI()
    local_settings = settings.model_copy(update={"cos_bucket": ""})
    with pytest.raises(prompt_optimizer.PromptOptimizerError) as exc:
        _optimize(db, local_settings, store, client, product_asset_ids=products)
    assert exc.value.code == "cos_required"
    assert not client.calls


def test_rejects_empty_and_too_long_model_prompts(settings, store, optimizer_db):
    db, products, _ = optimizer_db

    with pytest.raises(CoreAIError) as empty:
        _optimize(
            db,
            settings,
            store,
            FakeCoreAI({"prompt": "   "}),
            product_asset_ids=products,
        )
    assert empty.value.code == "prompt_optimizer_empty"

    with pytest.raises(CoreAIError) as too_long:
        _optimize(
            db,
            settings,
            store,
            FakeCoreAI({"prompt": "长" * 5001}),
            product_asset_ids=products,
        )
    assert too_long.value.code == "prompt_optimizer_too_long"

    with pytest.raises(CoreAIError) as image_error:
        _optimize(
            db, settings, store, FakeCoreAI({"error": "无法读取参考图"}),
            product_asset_ids=products,
        )
    assert image_error.value.code == "prompt_optimizer_agent_error"


def test_optimizer_accepts_5000_character_input_and_output(settings, store, optimizer_db):
    db, products, _ = optimizer_db
    prompt = "长" * 5000
    client = FakeCoreAI({"prompt": prompt})

    assert _optimize(db, settings, store, client, prompt=prompt, product_asset_ids=products) == prompt
    assert len(client.calls) == 1

    with pytest.raises(ValidationError):
        PromptOptimizeRequest(prompt=prompt + "长")





def _request_for(db, settings, store, client):
    from types import SimpleNamespace

    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(db=db, settings=settings, store=store, core_ai=client)
        )
    )


def test_route_returns_prompt_and_maps_configuration_error(settings, store, optimizer_db):
    from fastapi import HTTPException

    from app.routes import prompts as prompt_routes

    db, products, _ = optimizer_db
    client = FakeCoreAI({"prompt": "路由返回的提示词"})
    payload = PromptOptimizeRequest(
        prompt="",
        brand_mode="preset",
        brand_id="seejoy",
        product_asset_ids=products,
    )

    response = prompt_routes.optimize_prompt(
        payload, _request_for(db, settings, store, client)
    )
    assert response == {"prompt": "路由返回的提示词"}

    unconfigured = settings.model_copy(update={"core_ai_api_key": ""})
    with pytest.raises(HTTPException) as exc:
        prompt_routes.optimize_prompt(
            payload, _request_for(db, unconfigured, store, client)
        )
    assert exc.value.status_code == 503
