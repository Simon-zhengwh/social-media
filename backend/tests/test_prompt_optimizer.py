from __future__ import annotations

from datetime import datetime, timezone

import mongomock
import pytest

from app.schemas import PromptOptimizeRequest
from app.services import prompt_optimizer, storage
from app.services.core_ai import CoreAIError


class FakeCoreAI:
    def __init__(self, response: dict | None = None) -> None:
        self.response = response or {"prompt": "优化后的中文海报提示词"}
        self.calls: list[dict] = []

    def chat_json(self, messages, *, model=None, timeout_seconds=None):
        self.calls.append(
            {"messages": messages, "model": model, "timeout_seconds": timeout_seconds}
        )
        return self.response


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
        "workflow": "layered",
        "prompt": "",
        "brand_mode": "preset",
        "brand_id": "seejoy",
        "style_reference_asset_ids": [],
        "product_asset_ids": [],
        "platform_id": None,
        "placement_id": None,
        "merchant_info": None,
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
        merchant_info={"address": "119 W 31st St", "phone": "555-0100"},
    )

    assert result == "优化后的中文海报提示词"
    messages = client.calls[0]["messages"]
    content = messages[-1]["content"]
    image_count = sum(1 for item in content if item.get("type") == "image_url")
    assert image_count == 3
    text = "\n".join(item.get("text", "") for item in content if item.get("type") == "text")
    assert "当前提示词为空" in text
    assert "品牌：SEEJOY 喜聚" in text
    assert "使用毛笔主标题" in text
    assert "小红书" in text
    assert "不得出现地址" in text
    assert "素材图 1" in text
    assert "品牌参考图 1" in text


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
        workflow="whole_image",
    )

    content = client.calls[0]["messages"][-1]["content"]
    text = "\n".join(item.get("text", "") for item in content if item.get("type") == "text")
    assert "风格参考图 1" in text
    assert "只参考其配色、构图、字体气质和氛围" in text
    assert "使用毛笔主标题" not in text
    assert "整图直接生成" in text


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

    content = client.calls[0]["messages"][-1]["content"]
    text = "\n".join(item.get("text", "") for item in content if item.get("type") == "text")
    assert "提高毛笔标题占比" in text
    assert "只做高影响改进" in text


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
            FakeCoreAI({"prompt": "长" * 2001}),
            product_asset_ids=products,
        )
    assert too_long.value.code == "prompt_optimizer_too_long"





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
        workflow="layered",
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
