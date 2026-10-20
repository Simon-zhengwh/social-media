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

    def generate_image(self, prompt: str, *, size: str) -> bytes:
        self.generation_calls.append({"prompt": prompt, "size": size})
        colors = [(50, 90, 130), (210, 90, 25), (30, 120, 70)]
        color = colors[self._color_index % len(colors)]
        self._color_index += 1
        return self._image(color)

    def edit_image(self, prompt: str, references, *, size: str) -> bytes:
        reference_paths = [getattr(reference, "path", reference) for reference in references]
        self.edit_calls.append(
            {
                "prompt": prompt,
                "size": size,
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
):
    return whole_routes.create_task(
        WholeImageTaskCreate(
            prompt=prompt,
            ratio="1:1",
            product_asset_ids=product_asset_ids or [],
        ),
        _request(db, settings, store, client),
        BackgroundTasks(),
    )


def _generate(db, settings, store, client, prompt="夏日冰美式海报"):
    created = _create(db, settings, store, client, prompt)
    whole_service.run_generation(db, settings, store, client, created["id"])
    task = db.whole_tasks.find_one({"_id": created["id"]})
    return created["id"], task


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


def test_generation_creates_v1_without_parent(settings, store):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    task_id, task = _generate(db, settings, store, fake)

    versions = list(db.whole_versions.find({"task_id": task_id}))
    assert task["status"] == "succeeded"
    assert task["current_version"] == 1
    assert task["selected_version_id"] == versions[0]["_id"]
    assert len(versions) == 1
    assert versions[0]["parent_version_id"] is None
    assert versions[0]["operation"] == "generate"
    assert fake.generation_calls[0]["prompt"].endswith("夏日冰美式海报")
    assert "input_images" not in fake.generation_calls[0]


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
    assert "Original reference images: images 1-2" in prompt
    assert "Current design reference: image 3" in prompt


def test_second_edit_includes_successful_prior_instruction(settings, store):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    task_id, _ = _generate(db, settings, store, fake)
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
    assert "把日期改成本周末" in prompt
    assert "标题增加白色描边" in prompt
    assert prompt.index("把日期改成本周末") < prompt.index("标题增加白色描边")


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



def test_platform_whole_task_snapshots_rules_and_allows_contact_edit(settings, store):
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
            instruction="在底部补上地址和电话",
            source_version_id=version["_id"],
        ),
        request,
        BackgroundTasks(),
    )
    assert pending["status"] == "succeeded"
    assert pending["edit"]["status"] == "running"


def test_platform_generation_prompt_contains_contact_guidance_without_blocking(settings, store):
    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    request = _request(db, settings, store, fake)
    created = whole_routes.create_task(
        WholeImageTaskCreate(
            prompt="夏日新品海报，可以展示门店地址和电话",
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
    assert "不得出现地址" in fake.generation_calls[0]["prompt"]
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
