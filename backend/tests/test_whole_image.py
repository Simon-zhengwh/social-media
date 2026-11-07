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
from app.services.core_ai import AgentArtifact, AgentRunResult, CoreAIError


class FakeCoreAI:
    configured = True

    def __init__(self) -> None:
        self.generation_calls: list[dict] = []
        self.edit_calls: list[dict] = []
        self.agent_calls: list[dict] = []
        self.fail_edit = False
        self.fail_agent = True
        self._color_index = 0
        self.agent_artifacts = [
            AgentArtifact(
                "generated-image.png",
                "image/png",
                "https://example.com/agent-layer.png",
                "模型原生透明前景",
            ),
            AgentArtifact(
                "generated-image.png",
                "image/png",
                "https://example.com/agent-result.png",
                "最终成图",
            ),
            AgentArtifact(
                "editor.html",
                "text/html",
                "https://example.com/editor.html",
                "离线编辑器",
            ),
            AgentArtifact(
                "result-bundle.zip",
                "application/zip",
                "https://example.com/result.zip",
                "完整结果包",
            ),
        ]
        self.agent_downloads: list[str] = []
        self.agent_result_size: tuple[int, int] | None = None
        self.agent_image_overrides: dict[str, bytes] = {}
        self.agent_fail_downloads: set[str] = set()
        self.agent_fail_artifacts: set[str] = set()
        self.agent_output: str | None = None

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

    def upload_image_blob(self, data: bytes, *, content_type: str = "image/png") -> str:
        if self.fail_agent:
            raise CoreAIError("agent_disabled", "测试默认关闭 Agent 分支")
        self.agent_calls.append({"upload_bytes": data, "content_type": content_type})
        return "https://example.com/current-poster.png"

    def _transparent_image(self) -> bytes:
        image = Image.new("RGBA", (640, 640), (0, 0, 0, 0))
        for x in range(120, 520):
            for y in range(120, 220):
                image.putpixel((x, y), (180, 80, 40, 255))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def _sized_image(self, size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
        buffer = io.BytesIO()
        Image.new("RGB", size, color).save(buffer, format="PNG")
        return buffer.getvalue()

    def run_agent_detail(
        self,
        agent_id: str,
        input_text: str,
        *,
        timeout_seconds: int,
        poll_interval: float = 2.0,
    ) -> AgentRunResult:
        self.agent_calls.append(
            {"agent_id": agent_id, "input": input_text, "timeout_seconds": timeout_seconds}
        )
        return AgentRunResult(
            run_id="run-agent-1",
            status="COMPLETED",
            output=self.agent_output
            if self.agent_output is not None
            else (
                "![透明层](https://example.com/agent-layer.png)\n"
                "![成图](https://example.com/agent-result.png)"
            ),
            artifacts=self.agent_artifacts,
        )

    def download_image(self, url: str) -> bytes:
        self.agent_downloads.append(url)
        if url in self.agent_fail_downloads:
            raise CoreAIError("download_failed", "模拟图片下载失败")
        if url in self.agent_image_overrides:
            return self.agent_image_overrides[url]
        if url.endswith("agent-layer.png"):
            return self._transparent_image()
        if url.endswith("agent-result.png"):
            if self.agent_result_size is not None:
                return self._sized_image(self.agent_result_size, (120, 40, 180))
            return self._sized_image((640, 640), (120, 40, 180))
        raise CoreAIError("download_failed", "未知图片")

    def download_agent_file(self, url: str, *, content_type: str) -> bytes:
        self.agent_downloads.append(url)
        if url in self.agent_fail_artifacts:
            raise CoreAIError("artifact_failed", "模拟附件下载失败")
        if content_type == "text/html":
            return b"<!doctype html><h1>Editor</h1>"
        return b"PK\x03\x04bundle"

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
    assert "严格小于 30°" in client.generation_calls[0]["prompt"]
    assert "NEW_RULE" not in client.generation_calls[0]["prompt"]


@pytest.mark.parametrize("structured", [False, True])
def test_generation_uses_note_snapshot_and_excludes_hidden_fields(settings, store, structured):
    db = mongomock.MongoClient().db
    client = FakeCoreAI()
    req = _request(db, settings, store, client)
    db.brands.update_one({"_id": "seejoy"}, {"$set": {
        "schema_version": 2 if structured else 1,
        "style_fields": {"palette_notes": "绿", "typography": "UNIQUE_FONT", "subject_style": "实拍", "background": "纸感"} if structured else None,
        "summary": "HIDDEN_SUMMARY",
        "note": "VISIBLE_NOTE",
        "layout_guide": "LEGACY_CONTENT", "background_guide": "OLD_BACKGROUND",
    }})
    created = whole_routes.create_task(WholeImageTaskCreate(prompt="测试", brand_id="seejoy"), req, BackgroundTasks())
    db.brands.update_one({"_id": "seejoy"}, {"$set": {"note": "NEW_NOTE"}})
    whole_service.run_generation(db, settings, store, client, created["id"])
    prompt = client.generation_calls[0]["prompt"]
    for hidden in ("UNIQUE_FONT", "HIDDEN_SUMMARY", "LEGACY_CONTENT", "OLD_BACKGROUND", "NEW_NOTE"):
        assert hidden not in prompt
    assert prompt.count("VISIBLE_NOTE") == 1
    snapshot = db.whole_tasks.find_one({"_id": created["id"]})["brand"]
    assert snapshot["summary"] == "HIDDEN_SUMMARY"
    assert (snapshot["style_fields"] is not None) == structured
    assert snapshot["note"] == "VISIBLE_NOTE"


@pytest.mark.parametrize("optimized", [False, True])
def test_merchant_context_reaches_generation_and_edit_prompts(settings, store, product_png, optimized):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    product = _seed_product(db, settings, store, product_png, "dish.png")
    request = _request(db, settings, store, fake)
    db.brands.update_one({"_id": "seejoy"}, {"$set": {"merchant_id": "merchant"}})
    db.merchant_profiles.insert_one({
        "_id": "merchant", "name": "MERCHANT_NAME", "source_label": "HIDDEN_SOURCE_LABEL",
        "sections": [
            {"source_path": "brand.positioning", "text": "POSITIONING"},
            {"source_path": "locations.0.core_highlights", "text": "CORE_HIGHLIGHTS"},
            {"source_path": "locations.0.competitive_context.target_customers", "text": "TARGET_CUSTOMERS"},
            {"source_path": "brand.voice.description", "text": "VOICE_DESCRIPTION"},
            {"source_path": "locations.0.nap.address", "text": "HIDDEN_ADDRESS"},
        ],
    })
    created = whole_routes.create_task(
        WholeImageTaskCreate(
            prompt="新品海报", product_asset_ids=[product.id], prompt_optimized=optimized,
            brand_id="seejoy", brand_mode="preset",
        ),
        request, BackgroundTasks(),
    )

    assert "MERCHANT_NAME" in db.whole_tasks.find_one({"_id": created["id"]})["brand"]["merchant_context"]
    whole_service.run_generation(db, settings, store, fake, created["id"])

    generation_prompt = fake.edit_calls[0]["prompt"]
    for visible in ("商户背景参考：MERCHANT_NAME", "POSITIONING", "CORE_HIGHLIGHTS", "TARGET_CUSTOMERS", "VOICE_DESCRIPTION"):
        assert visible in generation_prompt
    assert "HIDDEN_SOURCE_LABEL" not in generation_prompt
    assert "HIDDEN_ADDRESS" not in generation_prompt

    version = db.whole_versions.find_one({"task_id": created["id"], "version": 1})
    fake.edit_calls.clear()
    fake.fail_agent = False
    instruction = "强化聚会氛围"
    whole_routes.create_edit(
        created["id"],
        WholeImageEditRequest(instruction=instruction, source_version_id=version["_id"]),
        request, BackgroundTasks(),
    )
    whole_service.run_edit(db, settings, store, fake, created["id"], instruction, version["_id"])

    for prompt in (fake.edit_calls[0]["prompt"], fake.agent_calls[-1]["input"]):
        assert "商户背景参考：MERCHANT_NAME" in prompt
        assert "POSITIONING" in prompt
        assert "HIDDEN_ADDRESS" not in prompt


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
    prepared = storage.prepare_upload(settings, store, data, name)
    db.assets.insert_one(
        {
            "_id": prepared.id,
            "source": "upload",
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
    prepared = storage.prepare_upload(settings, store, data, name)
    db.assets.insert_one(
        {
            "_id": prepared.id,
            "source": "upload",
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
            {"asset_id": product.id, "name": "dish.png", "role": "product"}
        ],
    }

    task_out = whole_routes.task_out(
        db, db.whole_tasks.find_one({"_id": created["id"]})
    )
    assert task_out["model_input"]["images"] == [
        {
            "asset_id": product.id,
            "name": "dish.png",
            "role": "product",
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
    assert "Do not take food content from style-only references" in prompt
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
        {"asset_id": product.id, "name": "person.png", "role": "product"}
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


@pytest.mark.parametrize("optimized", [False, True])
def test_generation_protects_food_photos_in_raw_and_optimized_prompts(
    settings, store, product_png, optimized
):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    product = _seed_product(db, settings, store, product_png, "side-dishes.png")
    request_text = "团餐海报，除了素材图之外禁止添加菜品，禁止重绘菜品"
    created = whole_routes.create_task(
        WholeImageTaskCreate(
            prompt=request_text, prompt_optimized=optimized,
            product_asset_ids=[product.id], brand_mode="none",
        ),
        _request(db, settings, store, fake), BackgroundTasks(),
    )

    whole_service.run_generation(db, settings, store, fake, created["id"])

    sent = fake.edit_calls[0]
    prompt = sent["prompt"]
    assert "full creative freedom" not in prompt
    assert "Food-photo preservation:" in prompt
    assert "crop, uniformly scale, or move" in prompt
    assert "plating, tableware, lighting, and relative positions" in prompt
    assert "Do not redraw, retouch, replace, rearrange, add, or remove food" in prompt
    assert "only when the user explicitly requests that specific change" not in prompt
    for constraint in ("严格小于 30°", "禁止 30° 及以上", "生成式补画或扩图", "不得编造", "摄影场景及其他内容素材"):
        assert constraint in prompt
    assert prompt.endswith(request_text)
    assert sent["reference_bytes"] == [store.get_bytes(product.prepared_path)]
    saved = db.whole_versions.find_one({"task_id": created["id"], "version": 1})
    assert saved["model_input"]["prompt"] == prompt


def test_text_edit_protects_food_in_both_gateway_and_agent_inputs(
    settings, store, product_png
):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    product = _seed_product(db, settings, store, product_png, "side-dishes.png")
    created = _create(
        db, settings, store, fake,
        prompt="原始文案：鲜切牛肉，搭配满桌招牌配菜。禁止重绘菜品",
        product_asset_ids=[product.id],
    )
    task_id = created["id"]
    whole_service.run_generation(db, settings, store, fake, task_id)
    v1 = db.whole_versions.find_one({"task_id": task_id, "version": 1})
    fake.edit_calls.clear()
    fake.fail_agent = False
    instruction = "把文案改成：除了新鲜牛肉，配菜也很新鲜"
    whole_routes.create_edit(
        task_id,
        WholeImageEditRequest(instruction=instruction, source_version_id=v1["_id"]),
        _request(db, settings, store, fake), BackgroundTasks(),
    )

    whole_service.run_edit(db, settings, store, fake, task_id, instruction, v1["_id"])

    gateway_prompt = fake.edit_calls[0]["prompt"]
    agent_prompt = fake.agent_calls[-1]["input"]
    for prompt in (gateway_prompt, agent_prompt):
        assert "Use the current poster as the editing base" in prompt
        assert "Food-photo preservation:" in prompt
        assert "Do not redraw, retouch, replace, rearrange, add, or remove food" in prompt
        assert "only when the user explicitly requests that specific change" not in prompt
        for constraint in ("严格小于 30°", "禁止 30° 及以上", "生成式补画或扩图", "不得编造", "摄影场景及其他内容素材"):
            assert constraint in prompt
        assert "For text-only edits, change only the requested lettering" in prompt
        assert instruction in prompt
        assert "原始文案：鲜切牛肉" not in prompt
        assert "style and layout only" not in prompt
        assert "do not use it as a pixel source" not in prompt
    assert "Image 2 is the current poster to edit." in gateway_prompt
    assert gateway_prompt.endswith(instruction)
    assert agent_prompt.endswith(instruction + "\n\nhttps://example.com/current-poster.png")
    version = db.whole_versions.find_one({"task_id": task_id, "version": 2})
    for output in version["outputs"]:
        expected = gateway_prompt if output["channel"] == "gateway" else agent_prompt
        assert output["model_input"]["prompt"] == expected


def test_edit_request_normalizes_and_persists_reference_asset_ids(settings, store, product_png):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    product = _seed_product(db, settings, store, product_png, "dish.png")
    reference = _seed_product(db, settings, store, product_png, "reference.png")
    created = _create(
        db,
        settings,
        store,
        fake,
        product_asset_ids=[product.id],
    )
    whole_service.run_generation(db, settings, store, fake, created["id"])
    version = db.whole_versions.find_one({"task_id": created["id"], "version": 1})

    payload = WholeImageEditRequest(
        instruction="参考这张图调整配色",
        source_version_id=version["_id"],
        reference_asset_ids=[reference.id, reference.id],
    )
    assert payload.reference_asset_ids == [reference.id]

    task = whole_routes.create_edit(
        created["id"],
        payload,
        _request(db, settings, store, fake),
        BackgroundTasks(),
    )

    assert task["edit"]["reference_asset_ids"] == [reference.id]
    assert task["messages"][-1]["attachments"] == [
        {
            "asset_id": reference.id,
            "name": "reference.png",
            "url": f"/api/assets/{reference.id}/file",
        }
    ]


def test_edit_reference_images_reach_both_branches(
    settings, store, product_png, monkeypatch
):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    product = _seed_product(db, settings, store, product_png, "dish.png")
    reference = _seed_product(db, settings, store, product_png, "reference.png")
    created = _create(
        db,
        settings,
        store,
        fake,
        product_asset_ids=[product.id],
    )
    whole_service.run_generation(db, settings, store, fake, created["id"])
    version = db.whole_versions.find_one({"task_id": created["id"], "version": 1})
    fake.edit_calls.clear()
    fake.agent_calls.clear()
    fake.fail_agent = False
    monkeypatch.setattr(
        whole_service.agent_image,
        "asset_public_url",
        lambda _db, _settings, _store, _client, asset_id, **_kwargs: (
            f"https://example.com/{asset_id}.png"
        ),
    )
    instruction = "参考这张图调整配色"

    whole_routes.create_edit(
        created["id"],
        WholeImageEditRequest(
            instruction=instruction,
            source_version_id=version["_id"],
            reference_asset_ids=[reference.id],
        ),
        _request(db, settings, store, fake),
        BackgroundTasks(),
    )
    whole_service.run_edit(
        db,
        settings,
        store,
        fake,
        created["id"],
        instruction,
        version["_id"],
        reference_asset_ids=[reference.id],
    )

    gateway_call = fake.edit_calls[-1]
    source = db.assets.find_one({"_id": version["output_asset_id"]})
    assert gateway_call["reference_names"][-2:] == [
        source["original_name"],
        "reference.png",
    ]
    assert "Image 3 is this turn's user-supplied edit reference 1." in gateway_call["prompt"]
    agent_input = fake.agent_calls[-1]["input"]
    assert agent_input.endswith(
        "https://example.com/"
        + str(version["output_asset_id"])
        + f".png\nhttps://example.com/{reference.id}.png"
    )
    task = db.whole_tasks.find_one({"_id": created["id"]})
    gateway_images = task["edit"]["branches"]["gateway"]["model_input"]["images"]
    agent_images = task["edit"]["branches"]["agent"]["model_input"]["images"]
    assert [image["role"] for image in gateway_images] == [
        "product",
        "current_design",
        "edit_reference",
    ]
    assert [image["role"] for image in agent_images] == [
        "edit_target",
        "edit_reference",
    ]
    expected_orientation = (
        "portrait" if source["height"] > source["width"]
        else "landscape" if source["width"] > source["height"]
        else "square"
    )
    assert "Canvas contract:" in agent_input
    assert f"{source['width']}×{source['height']}" in agent_input
    assert expected_orientation in agent_input
    assert "1:1" in agent_input
    agent_model_input = task["edit"]["branches"]["agent"]["model_input"]
    assert agent_model_input["parameters"]["width"] == source["width"]
    assert agent_model_input["parameters"]["height"] == source["height"]
    assert agent_model_input["parameters"]["orientation"] == expected_orientation
    assert agent_model_input["parameters"]["aspect_ratio"] == "1:1"
    assert task["edit"]["reference_asset_ids"] == [reference.id]


def test_agent_final_output_must_match_current_canvas(settings, store):
    db = mongomock.MongoClient().db
    buffer = io.BytesIO()
    Image.new("RGB", (1536, 1152), (80, 40, 20)).save(buffer, format="PNG")

    with pytest.raises(CoreAIError) as exc_info:
        whole_service.agent_image.save_png_asset(
            db,
            settings,
            store,
            key_prefix="aspect-mismatch",
            original_stem="aspect-mismatch",
            data=buffer.getvalue(),
            role="agent_result",
            target_size=(1072, 1440),
            require_aspect_match=True,
        )

    assert exc_info.value.code == "output_aspect_mismatch"
    assert db.assets.count_documents({}) == 0

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
    assert "Image 8 is the current poster to edit." in sent["prompt"]
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


def test_preset_brand_references_reach_model_without_hidden_guides(settings, store, product_png):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    brand_reference = storage.prepare_upload(
        settings, store, product_png, "brand-poster.png"
    )
    db.assets.insert_one({
        "_id": brand_reference.id,
        "source": "upload",
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

    assert "TEST LAYOUT GUIDE" not in fake.edit_calls[0]["prompt"]
    assert "TEST BACKGROUND GUIDE" not in fake.edit_calls[0]["prompt"]
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
    assert task["model_input_latest"]["images"][1]["role"] == "logo"


def test_task_accepts_asset_library_logo_with_preset_brand(settings, store, product_png):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    product = _seed_product(db, settings, store, product_png, "dish.png")
    logo = _seed_product(db, settings, store, product_png, "other-logo.png")

    created = whole_routes.create_task(
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
    assert created["input"]["logo_asset_id"] == logo.id


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
    assert selected["model_input"]["prompt"] == v1["model_input"]["prompt"]


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
    assert "Image 3 is the current poster to edit." in prompt
    assert "Use these originals only to check subject identity and details" in prompt
    assert "Use the current poster as the editing base" in prompt
    assert "do not reconstruct the current poster from them" in prompt
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
    assert snapshot["images"][-1]["role"] == "current_design"


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
    assert "Image 2 is the current poster to edit." in sent["prompt"]
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
    assert "Image 3 is the current poster to edit." in sent["prompt"]
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
    assert "Use these originals only to check subject identity and details" in prompt
    assert "Use the current poster as the editing base" in prompt
    source_asset = db.assets.find_one({"_id": v2["output_asset_id"]})
    original_bytes = store.get_bytes(original.prepared_path)
    assert fake.edit_calls[0]["reference_bytes"][0] == original_bytes
    assert fake.edit_calls[-1]["reference_bytes"] == [
        original_bytes, store.get_bytes(source_asset["path"])
    ]
    assert fake.edit_calls[-1]["reference_names"] == ["original.png", source_asset["original_name"]]
    assert db.whole_tasks.find_one({"_id": task_id})["edit"]["status"] == "idle"


@pytest.mark.parametrize("channel", ["gateway", "agent"])
@pytest.mark.parametrize("mode", ["RGB", "RGBA"])
def test_output_size_normalization_keeps_both_edges_of_a_landscape_image(
    settings, store, channel, mode
):
    from app.services import agent_image

    db = mongomock.MongoClient().db
    image = Image.new(mode, (120, 80), "white")
    image.paste("red", (0, 0, 10, 80))
    image.paste("blue", (110, 0, 120, 80))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    if channel == "gateway":
        saved = whole_service._save_output_asset(
            db, settings, store, task_id="task", version=2,
            data=buffer.getvalue(), target_size=(120, 160),
        )
    else:
        saved = agent_image.save_png_asset(
            db, settings, store, key_prefix="task-v2", original_stem="task-v2",
            data=buffer.getvalue(), role="agent_result", target_size=(120, 160),
        )

    with Image.open(io.BytesIO(store.get_bytes(saved["path"]))) as output:
        assert output.size == (120, 160)
        rgb = output.convert("RGB")
        assert rgb.getpixel((0, 80)) == (255, 0, 0)
        assert rgb.getpixel((119, 80)) == (0, 0, 255)
        if mode == "RGBA":
            assert output.getpixel((60, 0))[3] == 0
        else:
            assert rgb.getpixel((60, 0)) == (255, 255, 255)


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


def test_dual_edit_stores_three_outputs_and_requires_manual_selection(settings, store):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    fake.fail_agent = False
    task_id, _ = _generate(db, settings, store, fake)
    v1 = db.whole_versions.find_one({"task_id": task_id, "version": 1})
    request = _request(db, settings, store, fake)

    whole_routes.create_edit(
        task_id,
        WholeImageEditRequest(instruction="把背景换成橙色", source_version_id=v1["_id"]),
        request,
        BackgroundTasks(),
    )
    whole_service.run_edit(
        db, settings, store, fake, task_id, "把背景换成橙色", v1["_id"]
    )

    task = db.whole_tasks.find_one({"_id": task_id})
    v2 = db.whole_versions.find_one({"task_id": task_id, "version": 2})
    assert task["edit"]["status"] == "awaiting_selection"
    assert task["edit"]["outcome"] == "success"
    assert task["current_output_id"] is None
    assert [output["role"] for output in v2["outputs"]] == [
        "gateway_result",
        "agent_result",
        "agent_transparent",
    ]
    agent_output = next(output for output in v2["outputs"] if output["role"] == "agent_result")
    assert [item["kind"] for item in agent_output["artifacts"]] == ["html", "bundle"]
    assert [item["file_name"] for item in agent_output["artifacts"]] == [
        "editor.html",
        "result-bundle.zip",
    ]
    assert v2["outputs"][0]["artifacts"] == []
    assert sum(1 for output in v2["outputs"] if output["selectable"]) == 2
    assert fake.agent_downloads
    assert fake.agent_calls[-2]["upload_bytes"]
    assert fake.agent_calls[-1]["agent_id"] == settings.core_ai_image_agent_id
    assert fake.agent_calls[-1]["timeout_seconds"] == 1800
    agent_input = fake.agent_calls[-1]["input"]
    assert "Use the current poster as the editing base" in agent_input
    assert "Food-photo preservation:" in agent_input
    assert agent_input.endswith("把背景换成橙色\n\nhttps://example.com/current-poster.png")

    out = whole_routes.get_task(task_id, request)
    assert out["requires_output_selection"] is True
    assert out["current_output_id"] is None
    assert len(out["outputs"]) == 3

    transparent = next(output for output in out["outputs"] if output["role"] == "agent_transparent")
    with pytest.raises(HTTPException) as transparent_exc:
        whole_routes.select_task_output(task_id, v2["_id"], transparent["id"], request)
    assert transparent_exc.value.status_code == 422

    agent_final = next(output for output in out["outputs"] if output["role"] == "agent_result")
    selected = whole_routes.select_task_output(
        task_id,
        v2["_id"],
        agent_final["id"],
        request,
    )
    assert selected["current_output_id"] == agent_final["id"]
    assert selected["edit"]["status"] == "idle"
    assert selected["preview_url"] == agent_final["preview_url"]

    whole_routes.create_edit(
        task_id,
        WholeImageEditRequest(
            instruction="把标题再放大一点",
            source_version_id=v2["_id"],
            source_output_id=agent_final["id"],
        ),
        request,
        BackgroundTasks(),
    )
    whole_service.run_edit(
        db,
        settings,
        store,
        fake,
        task_id,
        "把标题再放大一点",
        v2["_id"],
        agent_final["id"],
    )
    assert db.whole_versions.count_documents({"task_id": task_id}) == 3


def test_edit_output_asset_is_normalized_to_source_dimensions(settings, store):
    db = mongomock.MongoClient().db
    source = io.BytesIO()
    Image.new("RGB", (320, 240), (10, 20, 30)).save(source, format="PNG")

    saved = whole_service._save_output_asset(
        db,
        settings,
        store,
        task_id="wtask-size",
        version=2,
        data=source.getvalue(),
        role="gateway",
        target_size=(1080, 1440),
    )

    asset = db.assets.find_one({"_id": saved["asset_id"]})
    assert saved["width"] == 1080
    assert saved["height"] == 1440
    with Image.open(io.BytesIO(store.get_bytes(asset["path"]))) as image:
        assert image.size == (1080, 1440)


def test_preset_brand_with_missing_logo_asset_still_creates_task(settings, store):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    db.brands.insert_one(
        {
            "_id": "stale-brand",
            "name": "Stale Brand",
            "short_name": "Stale",
            "summary": "test",
            "is_default": True,
            "layout_guide": "layout",
            "background_guide": "background",
            "logo_keywords": [],
            "logo_asset_ids": ["asset_deleted_logo"],
            "cover_asset_id": None,
            "reference_asset_ids": [],
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
    )

    created = whole_routes.create_task(
        WholeImageTaskCreate(
            prompt="无 Logo 海报",
            brand_id="stale-brand",
            brand_mode="preset",
        ),
        _request(db, settings, store, fake),
        BackgroundTasks(),
    )

    assert created["status"] == "queued"
    assert created["input"]["logo_asset_id"] is None
    stored = db.whole_tasks.find_one({"_id": created["id"]})
    assert stored["brand"]["logo_asset_ids"] == []


def test_preset_brand_accepts_existing_reference_with_legacy_null_kind(settings, store, product_png):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    product = _seed_product(db, settings, store, product_png, "dish.png")
    style = _seed_product(db, settings, store, product_png, "reference-1.jpg")
    db.assets.update_one({"_id": style.id}, {"$set": {"kind": None}})
    _seed_default_brand(db)
    db.brands.update_one(
        {"_id": "seejoy"},
        {"$set": {"reference_asset_ids": [style.id]}},
    )

    created = whole_routes.create_task(
        WholeImageTaskCreate(
            prompt="使用品牌参考图生成海报",
            product_asset_ids=[product.id],
            brand_id="seejoy",
            brand_mode="preset",
        ),
        _request(db, settings, store, fake),
        BackgroundTasks(),
    )
    whole_service.run_generation(db, settings, store, fake, created["id"])

    task = db.whole_tasks.find_one({"_id": created["id"]})
    assert task["status"] == "succeeded"
    assert fake.edit_calls[-1]["reference_names"] == ["dish.png", "reference-1.jpg"]


def test_artifact_object_keys_keep_their_extension(settings):
    assert (
        storage.object_key(settings, "artifacts", "wtask_v2_wart_abc.html")
        == "social-media/artifacts/wtask_v2_wart_abc.html"
    )
    assert (
        storage.object_key(settings, "artifacts", "wtask_v2_wart_abc.zip")
        == "social-media/artifacts/wtask_v2_wart_abc.zip"
    )
    assert (
        storage.object_key(settings, "outputs", "wtask_v2")
        == "social-media/outputs/wtask_v2.png"
    )
    with pytest.raises(storage.StorageError):
        storage.object_key(settings, "elsewhere", "x.png")


def test_agent_branch_rejects_mismatched_aspect_after_trying_candidates(settings, store):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    fake.fail_agent = False
    task_id, _ = _generate(db, settings, store, fake)
    v1 = db.whole_versions.find_one({"task_id": task_id, "version": 1})
    # Force the agent poster to 640x320 while the source canvas is 640x640.
    fake.agent_result_size = (640, 320)

    request = _request(db, settings, store, fake)
    whole_routes.create_edit(
        task_id,
        WholeImageEditRequest(instruction="改背景", source_version_id=v1["_id"]),
        request,
        BackgroundTasks(),
    )
    whole_service.run_edit(db, settings, store, fake, task_id, "改背景", v1["_id"])

    task = db.whole_tasks.find_one({"_id": task_id})
    assert task["edit"]["branches"]["agent"]["status"] == "failed"
    assert "画幅" in task["edit"]["branches"]["agent"]["error"]
    assert task["edit"]["outcome"] == "partial"


def _agent_artifacts(*specs):
    # specs: (file_name, content_type, url, title)
    return [AgentArtifact(*spec) for spec in specs]


def _run_agent_edit(db, settings, store, fake):
    task_id, _ = _generate(db, settings, store, fake)
    v1 = db.whole_versions.find_one({"task_id": task_id, "version": 1})
    request = _request(db, settings, store, fake)
    whole_routes.create_edit(
        task_id,
        WholeImageEditRequest(instruction="改背景", source_version_id=v1["_id"]),
        request,
        BackgroundTasks(),
    )
    whole_service.run_edit(db, settings, store, fake, task_id, "改背景", v1["_id"])
    task = db.whole_tasks.find_one({"_id": task_id})
    v2 = db.whole_versions.find_one({"task_id": task_id, "version": 2})
    return task, v2


def test_agent_layer_source_url_prefers_transparent_candidate(settings, store):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    fake.fail_agent = False
    transparent_url = "https://example.com/t-first.png"
    opaque_url = "https://example.com/o-second.png"
    pre_pick_url = "https://example.com/p-opaque.png"
    fake.agent_artifacts = _agent_artifacts(
        ("t-first.png", "image/png", transparent_url, "成图一"),
        ("o-second.png", "image/png", opaque_url, "成图二"),
        ("p-layer.png", "image/png", pre_pick_url, "透明层"),
    )
    # The transparent picker falls back to an opaque artifact, so only the
    # validated candidate in the final list may be used as the layer.
    fake.agent_image_overrides = {
        transparent_url: fake._transparent_image(),
        opaque_url: fake._sized_image((640, 640), (120, 40, 180)),
        pre_pick_url: fake._sized_image((640, 640), (10, 20, 30)),
    }
    fake.agent_output = (
        f"![一]({transparent_url})\n"
        f"![二]({opaque_url})"
    )

    _, v2 = _run_agent_edit(db, settings, store, fake)
    roles = [output["role"] for output in v2["outputs"]]
    assert roles == ["gateway_result", "agent_result", "agent_transparent"]
    layer = next(output for output in v2["outputs"] if output["role"] == "agent_transparent")
    assert layer["source_url"] == transparent_url


def test_agent_layer_found_after_poster_when_ordered_last(settings, store):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    fake.fail_agent = False
    transparent_url = "https://example.com/t-last.png"
    opaque_url = "https://example.com/o-first.png"
    fake.agent_artifacts = _agent_artifacts(
        ("o-first.png", "image/png", opaque_url, "成图一"),
        ("t-last.png", "image/png", transparent_url, "成图二"),
    )
    fake.agent_image_overrides = {
        opaque_url: fake._sized_image((640, 640), (120, 40, 180)),
        transparent_url: fake._transparent_image(),
    }
    # Opaque poster first, transparent layer second.
    fake.agent_output = (
        f"![一]({opaque_url})\n"
        f"![二]({transparent_url})"
    )

    _, v2 = _run_agent_edit(db, settings, store, fake)
    poster = next(output for output in v2["outputs"] if output["role"] == "agent_result")
    assert poster["source_url"] == opaque_url
    layer = next(output for output in v2["outputs"] if output["role"] == "agent_transparent")
    assert layer["source_url"] == transparent_url


def test_artifact_download_failure_keeps_poster(settings, store):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    fake.fail_agent = False
    fake.agent_fail_artifacts.add("https://example.com/editor.html")

    task, v2 = _run_agent_edit(db, settings, store, fake)
    assert task["edit"]["outcome"] == "success"
    poster = next(output for output in v2["outputs"] if output["role"] == "agent_result")
    assert [item["kind"] for item in poster["artifacts"]] == ["bundle"]
    assert "Agent 2 张" in task["messages"][-1]["content"]
    assert "附加 HTML/结果包" in task["messages"][-1]["content"]


def test_dead_candidate_url_falls_back_to_next(settings, store):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    fake.fail_agent = False
    dead_url = "https://example.com/dead-first.png"
    good_url = "https://example.com/good-second.png"
    fake.agent_artifacts = _agent_artifacts(
        ("dead-first.png", "image/png", dead_url, "成图一"),
        ("good-second.png", "image/png", good_url, "成图二"),
    )
    fake.agent_fail_downloads.add(dead_url)
    fake.agent_image_overrides = {
        good_url: fake._sized_image((640, 640), (120, 40, 180)),
    }
    fake.agent_output = (
        f"![一]({dead_url})\n"
        f"![二]({good_url})"
    )

    task, v2 = _run_agent_edit(db, settings, store, fake)
    assert task["edit"]["branches"]["agent"]["status"] == "completed"
    poster = next(output for output in v2["outputs"] if output["role"] == "agent_result")
    assert poster["source_url"] == good_url


def test_agent_artifacts_are_exposed_with_preview_and_download(settings, store):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    fake.fail_agent = False
    task_id, _ = _generate(db, settings, store, fake)
    v1 = db.whole_versions.find_one({"task_id": task_id, "version": 1})
    request = _request(db, settings, store, fake)
    whole_routes.create_edit(
        task_id,
        WholeImageEditRequest(instruction="换背景", source_version_id=v1["_id"]),
        request,
        BackgroundTasks(),
    )
    whole_service.run_edit(db, settings, store, fake, task_id, "换背景", v1["_id"])

    task = whole_routes.get_task(task_id, request)
    agent_output = next(
        output for output in task["outputs"] if output["role"] == "agent_result"
    )
    assert [item["kind"] for item in agent_output["artifacts"]] == ["html", "bundle"]
    html = agent_output["artifacts"][0]
    assert html["preview_url"] == f"/api/whole-image/artifacts/{html['id']}/preview"
    assert html["download_url"] == f"/api/whole-image/artifacts/{html['id']}/download"
    assert html["file_name"] == "editor.html"
    assert html["content_type"] == "text/html"
    assert html["size"] == len(b"<!doctype html><h1>Editor</h1>")
    assert html["title"] == "离线编辑器"
    assert agent_output["artifacts"][1]["preview_url"] is None
    gateway_output = next(
        output for output in task["outputs"] if output["role"] == "gateway_result"
    )
    assert gateway_output["artifacts"] == []
    branch_output = next(
        output
        for output in task["edit"]["branches"]["agent"]["outputs"]
        if output["role"] == "agent_result"
    )
    assert [item["kind"] for item in branch_output["artifacts"]] == ["html", "bundle"]

    web = FastAPI()
    web.include_router(whole_routes.router)
    web.state.db, web.state.settings, web.state.store = db, settings, store
    with TestClient(web) as client:
        preview = client.get(html["preview_url"])
        assert preview.status_code == 200
        csp = preview.headers["content-security-policy"]
        assert "sandbox allow-scripts allow-downloads;" in csp
        assert "allow-same-origin" not in csp
        assert "connect-src 'none'" in csp
        assert preview.headers["content-disposition"] == "inline"
        assert preview.headers["cache-control"] == "private, no-store"
        assert preview.headers["x-content-type-options"] == "nosniff"
        assert preview.headers["content-type"].startswith("text/html")
        assert b"Editor" in preview.content

        download = client.get(html["download_url"])
        assert download.status_code == 200
        assert download.headers["content-disposition"].startswith("attachment;")
        assert download.headers["content-type"].startswith("text/html")
        assert download.content == b"<!doctype html><h1>Editor</h1>"

        bundle = client.get(agent_output["artifacts"][1]["download_url"])
        assert bundle.status_code == 200
        assert bundle.content == b"PK\x03\x04bundle"
        assert bundle.headers["content-type"].startswith("application/zip")

        # Only HTML artifacts can be previewed; unknown ids are missing.
        missing_preview = client.get(
            "/api/whole-image/artifacts/wart_missing/preview"
        )
        assert missing_preview.status_code == 404
        missing_download = client.get(
            "/api/whole-image/artifacts/wart_missing/download"
        )
        assert missing_download.status_code == 404
        bundle_preview = client.get(
            f"/api/whole-image/artifacts/{agent_output['artifacts'][1]['id']}/preview"
        )
        assert bundle_preview.status_code == 422


def test_artifact_download_keeps_non_ascii_name_safe(settings, store):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    db = mongomock.MongoClient().db
    key = storage.object_key(settings, "artifacts", "wtask_cn_v2_wart_cn.zip")
    store.put_bytes(key, b"PK\x03\x04bundle", "application/zip")
    db.whole_artifacts.insert_one(
        {
            "_id": "wart_cn",
            "task_id": "wtask_cn",
            "version": 2,
            "kind": "bundle",
            "file_name": "海报结果包.zip",
            "content_type": "application/zip",
            "path": key,
            "bytes": 12,
            "sha256": "",
            "title": "完整结果包",
            "source_url": "https://example.com/result.zip",
            "created_at": datetime.now(timezone.utc),
        }
    )

    web = FastAPI()
    web.include_router(whole_routes.router)
    web.state.db, web.state.settings, web.state.store = db, settings, store
    with TestClient(web) as client:
        response = client.get("/api/whole-image/artifacts/wart_cn/download")

    assert response.status_code == 200
    assert response.content == b"PK\x03\x04bundle"
    assert response.headers["content-type"].startswith("application/zip")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''" in disposition
    assert "%E6%B5%B7%E6%8A%A5" in disposition
    fallback = disposition.split('filename="',1)[1].split('"',1)[0]
    assert fallback.isascii()
    assert fallback.endswith(".zip")


def test_outputs_without_artifacts_serialize_as_empty_list(settings, store):
    db = mongomock.MongoClient().db
    version = {
        "_id": "wver_legacy",
        "task_id": "wtask_legacy",
        "version": 1,
        "created_at": datetime.now(timezone.utc),
        "outputs": [
            {
                "id": "gateway_result",
                "channel": "gateway",
                "role": "gateway_result",
                "selectable": True,
            }
        ],
    }
    out = whole_routes._version_out(db, version)
    assert out["outputs"][0]["artifacts"] == []
    branch = whole_routes._branch_out(
        db,
        "wtask_legacy",
        {"status": "completed", "outputs": [{"role": "agent_result"}]},
    )
    assert branch["outputs"][0]["artifacts"] == []
def test_oversized_artifact_keeps_poster_and_reports_missing_title(settings, store):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    fake.fail_agent = False
    original = fake.download_agent_file

    def download_agent_file(url, *, content_type):
        if url == "https://example.com/editor.html":
            raise CoreAIError("artifact_too_large", "Agent产物超过体积限制", 413)
        return original(url, content_type=content_type)

    fake.download_agent_file = download_agent_file

    task, v2 = _run_agent_edit(db, settings, store, fake)
    assert task["edit"]["branches"]["agent"]["status"] == "completed"
    assert task["edit"]["outcome"] == "success"
    poster = next(output for output in v2["outputs"] if output["role"] == "agent_result")
    assert [item["kind"] for item in poster["artifacts"]] == ["bundle"]
    assert "未取得产物" in task["messages"][-1]["content"]
    assert "离线编辑器" in task["messages"][-1]["content"]


def test_poster_failure_cleans_up_saved_artifacts(settings, store):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    fake.fail_agent = False
    # Force every final candidate to fail the aspect gate so the poster never commits.
    fake.agent_result_size = (640, 320)

    task_id, _ = _generate(db, settings, store, fake)
    v1 = db.whole_versions.find_one({"task_id": task_id, "version": 1})
    request = _request(db, settings, store, fake)
    whole_routes.create_edit(
        task_id,
        WholeImageEditRequest(instruction="改背景", source_version_id=v1["_id"]),
        request,
        BackgroundTasks(),
    )
    whole_service.run_edit(db, settings, store, fake, task_id, "改背景", v1["_id"])

    task = db.whole_tasks.find_one({"_id": task_id})
    assert task["edit"]["branches"]["agent"]["status"] == "failed"
    assert db.whole_artifacts.count_documents({}) == 0
    artifact_dir = settings.storage_path / "objects" / "social-media" / "artifacts"
    assert not list(artifact_dir.glob("**/*")) if artifact_dir.exists() else True
