from __future__ import annotations

import io
import json
import uuid
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from threading import Event, Lock
from types import SimpleNamespace

import httpx
import mongomock
import pytest
from fastapi import BackgroundTasks, HTTPException
from PIL import Image, ImageDraw

from app.config import Settings
from app.routes import whole_image as whole_routes
from app.schemas import PosterSeriesRunCreate, SeriesModelGroup, WholeImageTaskCreate
from app.services import poster_finalizations, poster_series, poster_series_round
from app.services.core_ai import AgentArtifact, AgentRunResult, CoreAIClient
from app.services.object_store import LocalObjectStore
from app.services.scoped_db import ScopedDatabase

UTC = timezone.utc
MERCHANT = "merchant-a"


def _settings(tmp_path, **overrides) -> Settings:
    values = dict(
        storage_dir=str(tmp_path / "storage"),
        core_ai_api_key="test-key",
        poster_series_enabled=True,
        core_ai_series_agent_id="series-agent-test",
    )
    values.update(overrides)
    return Settings(**values)


def _png(width: int = 120, height: int = 120, color=(180, 90, 50), mode="RGB") -> bytes:
    image = Image.new(mode, (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _solid_mark_png(size: tuple[int, int], color, *, inset: int) -> bytes:
    """A transparent Logo file whose visible ink is one flat colour."""
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((inset, inset, size[0] - inset - 1, size[1] - inset - 1), fill=color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _contrast_ratio(first, second) -> float:
    def luminance(color) -> float:
        channels = []
        for value in color:
            value = value / 255
            channels.append(value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4)
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _scaled_mark(box: dict, logo: bytes):
    """The Logo exactly as ``composite_logo`` scales it, plus its placement."""
    with Image.open(io.BytesIO(logo)) as source:
        scaled = source.convert("RGBA")
        scaled.thumbnail((box["width"], box["height"]), Image.Resampling.LANCZOS)
        x = box["x"] + (box["width"] - scaled.width) // 2
        y = box["y"] + (box["height"] - scaled.height) // 2
        return scaled, x, y


def _opaque_ink_pixel(scaled: Image.Image) -> tuple[int, int]:
    """A fully opaque ink pixel of the scaled Logo, where compositing changes nothing."""
    alpha = scaled.getchannel("A")
    for x in range(scaled.width // 4, scaled.width * 3 // 4):
        for y in range(scaled.height // 4, scaled.height * 3 // 4):
            if alpha.getpixel((x, y)) == 255:
                return x, y
    raise AssertionError("the test Logo has no opaque ink")


def _logo_png() -> bytes:
    image = Image.new("RGBA", (120, 40), (0, 0, 0, 0))
    for x in range(10, 110):
        for y in range(10, 30):
            image.putpixel((x, y), (220, 20, 20, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _asset(db, store, asset_id: str, name: str, *, color=(180, 90, 50)) -> dict:
    relative = f"assets/{asset_id}.png"
    store.put_bytes(relative, _png(color=color), "image/png")
    doc = {
        "_id": asset_id,
        "original_name": name,
        "path": relative,
        "prepared_path": relative,
        "mime_type": "image/png",
        "width": 120,
        "height": 120,
        "sha256": asset_id,
    }
    db.assets.insert_one(doc)
    return doc


class FakeCoreAI:
    configured = True

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail
        self.upload_count = 0
        self.trigger_count = 0
        self.results: dict[str, object] = {}
        self.last_trace: dict = {}
        self.edit_calls: list[dict] = []
        self.spans: list[dict] = []

    def _image(self) -> bytes:
        # Non-uniform output: the logo test can prove pixels outside the box survive.
        image = Image.new("RGB", (640, 640), (240, 240, 240))
        for x in range(0, 320):
            for y in range(0, 320):
                image.putpixel((x, y), (30, 90, 140))
        for x in range(320, 640):
            for y in range(320, 640):
                image.putpixel((x, y), (200, 90, 40))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def upload_image_blob(self, data: bytes, *, content_type: str = "image/png") -> str:
        self.upload_count += 1
        return f"https://core-ai.test/blob/{self.upload_count}.png"

    def _agent_result(self, input_text: str):
        from app.services.core_ai import AgentArtifact, AgentRunResult

        payload = json.loads(input_text)
        self.calls.append({"kind": payload.get("operation"), "payload": payload})
        if self.fail:
            raise RuntimeError("模拟 Agent 失败")
        operation = payload.get("operation")
        if operation == "poster-series-plan":
            pages = []
            content = [item for item in payload["content_pool"] if item["role"] == "content"]
            for page in payload["pages"]:
                pages.append({
                    "page_id": page["page_id"],
                    "role": page["role"],
                    "title": f"标题{page['order'] + 1}",
                    "intent": f"介绍第{page['order'] + 1}页",
                    "content_refs": [{
                        "asset_id": content[min(page["order"], len(content) - 1)]["asset_id"],
                        "target": "内容",
                    }],
                    "style_reference_ids": [
                        item["asset_id"] for item in payload["content_pool"]
                        if item["role"] == "style_reference"
                    ],
                })
            result = {
                "group_id": payload["group"]["group_id"],
                "pages": pages,
                "logo_box": {"x": 100, "y": 200, "width": 300, "height": 90},
                "logo_treatment": "Small cocoa-brown tab integrated into the title block",
            }
            return AgentRunResult(
                run_id="", status="COMPLETED",
                output=json.dumps(result, ensure_ascii=False), artifacts=[],
            )
        content = [
            {"asset_id": item["asset_id"], "target": item["name"]}
            for item in payload["content_pool"] if item["role"] == "content"
        ][:1]
        styles = [item["asset_id"] for item in payload["content_pool"] if item["role"] == "style_reference"]
        cover = (payload.get("cover") or {}).get("asset_id")
        prompt = f"agent prompt for {payload['page']['role']}"
        result = {
            "page_id": payload["page"]["page_id"],
            "role": payload["page"]["role"],
            "model": payload["requested_model"],
            "prompt": prompt,
            "content_refs": content,
            "style_reference_ids": styles,
            "cover_asset_id": cover,
        }
        selected = {item["asset_id"] for item in content} | set(styles)
        if cover:
            selected.add(cover)
        urls = [
            item["url"] for item in payload["content_pool"]
            if item["asset_id"] in selected
        ]
        if cover:
            urls.append(payload["cover"]["url"])
        self.last_trace = {
            "status": "COMPLETED",
            "transcript": [{
                "tool": "generate_image",
                "arguments": {
                    "model": payload["requested_model"],
                    "quality": "xhigh",
                    "prompt": prompt,
                    "input_images": json.dumps([{"url": url} for url in urls]),
                },
            }],
        }
        return AgentRunResult(
            run_id="", status="COMPLETED", output=json.dumps(result, ensure_ascii=False),
            artifacts=[AgentArtifact(
                file_name="page.png", content_type="image/png",
                download_url="https://core-ai.test/artifacts/page.png",
            )],
        )

    def trigger_agent(self, agent_id, input_text, *, timeout_seconds):
        self.trigger_count += 1
        run_id = f"run-{self.trigger_count}"
        result = self._agent_result(input_text)
        self.results[run_id] = result
        return run_id

    def wait_agent_detail(self, run_id, *, timeout_seconds, poll_interval=2.0):
        return self.results[run_id]

    def run_agent_detail(self, agent_id, input_text, *, timeout_seconds, poll_interval=2.0):
        return self.wait_agent_detail(
            self.trigger_agent(agent_id, input_text, timeout_seconds=timeout_seconds),
            timeout_seconds=timeout_seconds,
        )

    def get_agent_run(self, run_id: str):
        return self.last_trace

    def get_agent_trace_spans(self, trace_id: str):
        return self.spans

    def download_agent_images(self, result):
        from app.services.core_ai import DownloadedAgentImage

        return [DownloadedAgentImage(
            data=self._image(), source_url="https://core-ai.test/artifacts/page.png",
            file_name="page.png", content_type="image/png",
        )]

    def edit_image(self, prompt, refs, *, size, model):
        self.edit_calls.append({"prompt": prompt, "refs": refs, "size": size, "model": model})
        return self._image()


def _build_task(payload: WholeImageTaskCreate) -> dict:
    now = datetime.now(UTC)
    task_id = f"wtask_{uuid.uuid4().hex}"
    width, height = (1080, 1350)
    return {
        "_id": task_id,
        "status": "queued",
        "stage": "pending",
        "progress": 0,
        "input": {
            "image_model": payload.image_model,
            "prompt": payload.prompt,
            "prompt_optimized": payload.prompt_optimized,
            "ratio": payload.ratio,
            "product_asset_ids": [],
            "reference_asset_ids": list(payload.reference_asset_ids or []),
            "brand_id": payload.brand_id,
            "brand_mode": payload.brand_mode or "none",
            "logo_asset_id": payload.logo_asset_id,
            "style_reference_asset_ids": list(payload.style_reference_asset_ids),
            "candidate_count": 1,
            "candidate_batch_id": None,
            "generation_workspace_id": payload.generation_workspace_id,
            "candidate_index": 0,
            "platform_id": payload.platform_id,
            "placement_id": payload.placement_id,
        },
        "platform": None,
        "placement": None,
        "brand": {"id": payload.brand_id, "name": "SEEJOY", "layout_guide": "", "background_guide": ""},
        "merchant_context": "",
        "brand_rules": None,
        "canvas": {"width": width, "height": height},
        "provider": {"name": "core-ai", "model": payload.image_model, "status": "queued"},
        "generation": {"status": "queued", "error": None},
        "selected_version_id": None,
        "current_version_id": None,
        "current_version": 0,
        "output_asset_id": None,
        "messages": [{"id": f"msg_{uuid.uuid4().hex}", "role": "user",
                      "content": payload.prompt, "created_at": now,
                      "version_id": None, "version": None, "status": "done"}],
        "edit": {"status": "idle", "instruction": None, "source_version_id": None, "error": None},
        "error": None,
        "created_at": now,
        "updated_at": now,
    }


def _payload(**overrides) -> PosterSeriesRunCreate:
    values = dict(
        submit_id="submit-12345678",
        prompt="系列海报",
        image_groups=[SeriesModelGroup(image_model="gpt-image-2.5-sunburst", group_count=1)],
        pages_per_group=None,
        auto_generate_interiors=False,
    )
    values.update(overrides)
    return PosterSeriesRunCreate(**values)


def _make_run(db, settings, store, payload, launched: list[str] | None = None) -> dict:
    launched = launched if launched is not None else []

    def _launch(task_id: str) -> None:
        launched.append(task_id)

    return poster_series.create_run(
        db, settings, "workspace-1", payload,
        build_task=_build_task, launch=_launch, merchant_id=MERCHANT,
    )


def _cover_version(db, store, group, *, base_asset="asset_cover_base", final_asset=None):
    cover_page = db.whole_series_pages.find_one({"_id": group["cover_page_id"]})
    task_id = cover_page["whole_task_id"]
    final_asset = final_asset or base_asset
    now = datetime.now(UTC)
    version_id = f"wver_{uuid.uuid4().hex}"
    output = {
        "id": "series_base" if final_asset == base_asset else "series_final",
        "channel": "gateway",
        "role": "series_final",
        "label": "成品",
        "asset_id": final_asset,
        "width": 1080,
        "height": 1350,
        "selectable": True,
        "model_input": {"images": []},
        "created_at": now,
    }
    db.whole_versions.insert_one({
        "_id": version_id, "task_id": task_id, "version": 1,
        "parent_version_id": None, "output_asset_id": final_asset,
        "outputs": [output], "instruction": "cover", "operation": "series_generate",
        "width": 1080, "height": 1350, "model_input": {"images": []},
        "series_base_asset_id": base_asset, "series_content_refs": ["asset-1"],
        "created_at": now,
    })
    db.whole_tasks.update_one({"_id": task_id}, {"$set": {
        "status": "succeeded", "stage": "done", "progress": 100,
        "current_version_id": version_id, "selected_version_id": version_id,
        "current_version": 1, "current_output_id": output["id"],
        "output_asset_id": final_asset,
        "generation.status": "completed", "series.status": "succeeded",
    }})
    db.whole_series_pages.update_one({"_id": cover_page["_id"]}, {"$set": {"status": "succeeded"}})
    db.whole_series_groups.update_one({"_id": group["_id"]}, {"$set": {"status": "running"}})
    return version_id


# --------------------------------------------------------------------------- PS-02

def test_page_count_semantics():
    assert poster_series.resolve_page_count("做 4 张海报") == 4
    assert poster_series.resolve_page_count("每组 4 张") == 4
    assert poster_series.resolve_page_count("做 2 张内页") == 3
    assert poster_series.resolve_page_count("介绍 5 种甜品") == 2
    assert poster_series.resolve_page_count("做三张") == 3
    assert poster_series.resolve_page_count("介绍 5 种甜品", explicit=3) == 3


# --------------------------------------------------------------------------- PS-01

def test_workspace_mode_locks_first_submission_and_infers_legacy():
    db = mongomock.MongoClient().db
    settings = _settings.__wrapped__ if False else Settings(poster_series_enabled=True)
    poster_series.claim_workspace_mode(db, "w1", "single")
    with pytest.raises(poster_series.SeriesError) as error:
        poster_series.claim_workspace_mode(db, "w1", "series")
    assert error.value.code == "workspace_mode_locked"
    assert poster_series.get_workspace_mode(db, "w1", settings)["mode"] == "single"
    # A legacy workspace without a mode record opens as a single poster.
    db.whole_tasks.insert_one({"_id": "legacy", "input": {"generation_workspace_id": "w2"}})
    legacy = poster_series.get_workspace_mode(db, "w2", settings)
    assert legacy["mode"] == "single" and legacy["locked"] is False
    # A series run created before mode records is inferred as series.
    db.whole_series_groups.insert_one({"_id": "g", "workspace_id": "w3"})
    assert poster_series.get_workspace_mode(db, "w3", settings)["mode"] == "series"


# --------------------------------------------------------------------------- PS-03/04

def test_multi_model_groups_plan_pages_and_keep_pool(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    for index in range(1, 6):
        _asset(db, store, f"asset-{index}", f"图{index}")
    payload = _payload(
        prompt="每组 4 张，用 @图片1 做封面，@图片2 和 @图片4 做介绍。",
        reference_asset_ids=[f"asset-{index}" for index in range(1, 6)],
        image_groups=[
            SeriesModelGroup(image_model="gpt-image-2.5-sunburst", group_count=2),
            SeriesModelGroup(image_model="seedream-5-pro-image-to-image", group_count=1),
        ],
    )
    launched: list[str] = []
    result = _make_run(db, settings, store, payload, launched)
    groups = result["groups"]
    assert len(groups) == 3
    assert [group["image_model"] for group in groups] == [
        "gpt-image-2.5-sunburst", "gpt-image-2.5-sunburst",
        "seedream-5-pro-image-to-image",
    ]
    for group in groups:
        assert group["page_count"] == 4
        assert [page["role"] for page in group["pages"]] == ["cover", "detail", "detail", "detail"]
        cover = group["pages"][0]
        assert [item["asset_id"] for item in cover["content_refs"]] == ["asset-1"]
        assert [item["asset_id"] for item in group["pages"][1]["content_refs"]] == ["asset-2"]
        assert [item["asset_id"] for item in group["pages"][2]["content_refs"]] == ["asset-4"]
        # Detail pages are planned but not launched before the cover is ready.
        assert group["pages"][1]["whole_task_id"] is None
    assert len(launched) == 3
    # The unmentioned uploads remain in the task asset pool, not on any page.
    task = db.whole_tasks.find_one({"_id": launched[0]})
    assert task["input"]["reference_asset_ids"] == ["asset-1"]
    # Re-posting the same submit id restores the same groups and queues nothing new.
    again = _make_run(db, settings, store, payload, launched)
    assert [group["id"] for group in again["groups"]] == [group["id"] for group in groups]
    assert len(launched) == 3


# --------------------------------------------------------------------------- PS-05/06/07

def test_manual_and_auto_interiors_use_cover_base_once(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    _asset(db, store, "asset-1", "封面图")
    _asset(db, store, "asset-2", "介绍图")
    payload = _payload(
        prompt="做 3 张", pages_per_group=3,
        reference_asset_ids=["asset-1", "asset-2"],
    )
    launched: list[str] = []
    result = _make_run(db, settings, store, payload, launched)
    group = result["groups"][0]
    db_group = db.whole_series_groups.find_one({"_id": group["id"]})
    version_id = _cover_version(db, store, db_group, base_asset="asset_cover_base")

    manual = poster_series.start_interiors(
        db, settings, group["id"], cover_version_id=version_id,
        launch=lambda task_id: launched.append(task_id), merchant_id=MERCHANT,
    )
    details = [page for page in manual["pages"] if page["role"] == "detail"]
    assert all(page["whole_task_id"] for page in details)
    assert all(page["cover_reference"]["asset_id"] == "asset_cover_base" for page in details)
    assert all(page["cover_reference"]["version_id"] == version_id for page in details)
    assert len(launched) == 3  # cover + two interiors
    # Re-clicking the button does not create a second task per page.
    poster_series.start_interiors(
        db, settings, group["id"], cover_version_id=version_id,
        launch=lambda task_id: launched.append(task_id), merchant_id=MERCHANT,
    )
    assert len(launched) == 3


def test_legacy_auto_interiors_launch_once_after_cover(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    _asset(db, store, "asset-1", "封面图")
    _asset(db, store, "asset_cover_base", "封面底图")
    payload = _payload(
        prompt="系列", pages_per_group=2,
        reference_asset_ids=["asset-1"],
    )
    result = _make_run(db, settings, store, payload)
    group = result["groups"][0]
    db.whole_series_groups.update_one({"_id": group["id"]}, {"$set": {"auto_generate_interiors": True}})
    db_group = db.whole_series_groups.find_one({"_id": group["id"]})
    version_id = _cover_version(db, store, db_group)
    version = db.whole_versions.find_one({"_id": version_id})
    client = FakeCoreAI()
    poster_series.on_cover_complete(db, settings, store, client, group["id"], version)
    detail = db.whole_series_pages.find_one({"group_id": group["id"], "role": "detail"})
    assert detail["whole_task_id"]
    assert db.whole_series_groups.find_one({"_id": group["id"]})["interiors_started_at"]
    # A repeated callback is a no-op: the detail task count stays at one.
    poster_series.on_cover_complete(db, settings, store, client, group["id"], version)
    assert db.whole_tasks.count_documents({"series.group_id": group["id"], "series.role": "detail"}) == 1


def test_agent_series_matches_same_named_pngs_by_reported_url():
    first = AgentArtifact("generated-image.png", "image/png", "https://core-ai.test/first")
    second = AgentArtifact("generated-image.png", "image/png", "https://core-ai.test/second")
    manifest = {"groups": [{"id": "group", "model": "gpt-image-2.5-sunburst", "images": [
        {"image_url": first.download_url, "content_refs": [{"asset_id": "first-photo"}]},
        {"image_url": second.download_url, "content_refs": [{"asset_id": "second-photo"}]},
    ]}]}
    result = AgentRunResult(
        run_id="run", status="COMPLETED",
        output=f"```json\n{json.dumps(manifest)}\n```\n![]({first.download_url})",
        artifacts=[second, first, second, first],
    )
    group = {"_id": "group", "image_model": "gpt-image-2.5-sunburst", "content_pool": ["first-photo", "second-photo"]}

    images = poster_series_round._read_manifest(result, [group])["group"]["images"]

    assert [item["artifact"] for item in images] == [first, second]
    assert [item["content_refs"][0]["asset_id"] for item in images] == ["first-photo", "second-photo"]


@pytest.mark.parametrize("query", ["", "?snapshot=older"])
def test_agent_series_recognizes_registered_blob_redirect(tmp_path, query):
    first = AgentArtifact("cover.png", "image/png", "https://core-ai.test/api/public/artifacts/cover/content")
    second = AgentArtifact("detail.png", "image/png", "https://core-ai.test/api/public/artifacts/detail/content")
    intermediate = AgentArtifact("generated-image.png", "image/png", "https://core-ai.test/api/public/artifacts/draft/content")
    blob = "https://artifacts.blob.core.windows.net/output/"
    targets = {
        first.download_url: blob + "first.png?sp=r&sv=2024-11-04&sr=b&sig=secret",
        second.download_url: blob + "second.png?sp=r&sv=2024-11-04&sr=b&sig=secret",
        intermediate.download_url: blob + "draft.png?sp=r&sv=2024-11-04&sr=b&sig=secret",
    }

    def handler(request):
        assert "Authorization" not in request.headers
        return httpx.Response(307, headers={"Location": targets[str(request.url)]})

    manifest = {"groups": [{"id": "group", "model": "gpt-image-2.5-sunburst", "images": [
        {"image_url": blob + "first.png" + query, "content_refs": [{"asset_id": "first-photo"}]},
        {"image_url": blob + "second.png", "content_refs": [{"asset_id": "second-photo"}]},
    ]}]}
    result = AgentRunResult("run", "COMPLETED", json.dumps(manifest), [second, intermediate, first])
    group = {"_id": "group", "image_model": "gpt-image-2.5-sunburst", "content_pool": ["first-photo", "second-photo"]}
    client = CoreAIClient(_settings(tmp_path, core_ai_server_base_url="https://core-ai.test"),
                          transport=httpx.MockTransport(handler))

    def read():
        return poster_series_round._read_manifest(result, [group], artifact_redirect=client.agent_artifact_redirect)

    if query:
        with pytest.raises(poster_series.SeriesError) as error:
            read()
        assert error.value.code == "agent_image_missing"
    else:
        assert [item["artifact"] for item in read()["group"]["images"]] == [first, second]


def test_agent_series_keeps_known_style_out_of_photo_replacement():
    artifact = AgentArtifact("image.png", "image/png", "https://core-ai.test/image.png")
    image = {"image_url": artifact.download_url, "content_refs": [
        {"asset_id": "style-1"}, {"asset_id": "photo-1", "target": "甜品"},
    ]}
    manifest = {"groups": [{"id": "group", "model": "gpt-image-2.5-sunburst", "images": [image]}]}
    group = {"_id": "group", "image_model": "gpt-image-2.5-sunburst", "content_pool": ["photo-1"]}
    result = AgentRunResult("run", "COMPLETED", json.dumps(manifest), [artifact])

    parsed = poster_series_round._read_manifest(result, [group], style_reference_ids={"style-1"})

    assert parsed["group"]["images"][0]["content_refs"] == [{"asset_id": "photo-1", "target": "甜品"}]
    image["content_refs"].append({"asset_id": "unprovided"})
    rejected = replace(result, output=json.dumps(manifest))
    with pytest.raises(poster_series.SeriesError, match="素材池以外"):
        poster_series_round._read_manifest(rejected, [group], style_reference_ids={"style-1"})


def test_round_queues_every_page_and_interior_runs_before_cover_finishes(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    _asset(db, store, "asset-1", "甜品图")
    payload = _payload(
        prompt="做 3 张", auto_generate_interiors=True, reference_asset_ids=["asset-1"],
        image_groups=[SeriesModelGroup(image_model="gpt-image-2.5-sunburst", group_count=2)],
    )
    launched: list[str] = []
    result = _make_run(db, settings, store, payload, launched)
    assert len(launched) == 6
    assert all(page["status"] == "queued" and page["whole_task_id"]
               for group in result["groups"] for page in group["pages"])
    _make_run(db, settings, store, payload, launched)
    assert len(launched) == 6

    class ConcurrentClient(FakeCoreAI):
        def __init__(self):
            super().__init__()
            self.lock = Lock()
            self.detail_started = {group["id"]: Event() for group in result["groups"]}
            self.covers: dict[str, str] = {}
            self.traces: dict[str, dict] = {}
            self.overlapping_covers: list[str] = []

        def trigger_agent(self, agent_id, input_text, *, timeout_seconds):
            with self.lock:
                run_id = super().trigger_agent(agent_id, input_text, timeout_seconds=timeout_seconds)
                self.traces[run_id] = self.last_trace
                request = json.loads(input_text)
                if request["operation"] == "poster-series-page":
                    group_id = request["group"]["group_id"]
                    if request["page"]["role"] == "cover":
                        self.covers[run_id] = group_id
                    else:
                        self.detail_started[group_id].set()
                return run_id

        def wait_agent_detail(self, run_id, *, timeout_seconds, poll_interval=2.0):
            group_id = self.covers.get(run_id)
            if group_id:
                assert self.detail_started[group_id].wait(5), "内页应在封面返回前开始"
                self.overlapping_covers.append(group_id)
            result = super().wait_agent_detail(run_id, timeout_seconds=timeout_seconds, poll_interval=poll_interval)
            return replace(result, run_id=run_id)

        def get_agent_run(self, run_id):
            return self.traces[run_id]

    client = ConcurrentClient()
    poster_series.run_round_generation(db, settings, store, client, launched)
    assert len(client.overlapping_covers) == 2
    assert len([call for call in client.calls if call["kind"] == "poster-series-plan"]) == 2
    for group in poster_series.get_workspace_series(db, "workspace-1", settings)["groups"]:
        assert group["complete"] and len(group["pages"]) == 3
    for call in client.calls:
        request = call["payload"]
        if call["kind"] == "poster-series-page":
            assert request["group_plan"]["group_id"] == request["group"]["group_id"]
            assert request["cover"] is None


@pytest.mark.parametrize(("logo_clause", "logo_position", "expected_position"), [
    ("", None, None),
    ("Logo 放左上角。", None, "top-left"),
    ("Logo 放左上角并放大一点。", "bottom-right", "bottom-right"),
])
def test_new_series_request_calls_agent_once_and_accepts_its_image_counts(
    tmp_path, logo_clause, logo_position, expected_position,
):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    _asset(db, store, "asset-1", "参考图")
    _asset(db, store, "logo-1", "Logo")
    _asset(db, store, "style-1", "品牌风格参考")

    def build_with_brand_style(payload):
        template = _build_task(payload)
        template["brand"]["reference_asset_ids"] = ["style-1"]
        return template
    prompt = "用原照做几张统一风格的图，内容自由。" + logo_clause
    payload = _payload(prompt=prompt, ratio="4:5", reference_asset_ids=["asset-1"], logo_asset_id="logo-1",
                       logo_position=logo_position, image_groups=[
        SeriesModelGroup(image_model="gpt-image-2.5-sunburst", group_count=1),
        SeriesModelGroup(image_model="minimax-h3-image-edit", group_count=1),
    ])
    launched = []
    for _ in range(2):
        snapshot = poster_series_round.create_run(db, settings, "workspace-agent", payload,
                                                 build_task=build_with_brand_style, launch=launched.append)
        assert len(snapshot["groups"]) == 2
        assert all(group["page_count"] == 0 and not group["pages"] for group in snapshot["groups"])
    assert len(launched) == 1
    assert db.whole_tasks.count_documents({}) == 0

    class SeriesClient(FakeCoreAI):
        def trigger_agent(self, agent_id, input_text, *, timeout_seconds):
            self.calls.append(json.loads(input_text))
            return "agent-once"

        def wait_agent_detail(self, run_id, *, timeout_seconds):
            artifacts, groups = [], []
            for group, count in zip(self.calls[0]["groups"], [1, 3], strict=True):
                images = []
                for index in range(count):
                    filename = f"{group['id']}-{index}.png"
                    images.append({"file_name": filename, "content_refs": [{"asset_id": "asset-1", "target": str(index)}]})
                    artifacts.append(AgentArtifact(filename, "image/png", f"https://core-ai.test/{filename}"))
                options = self.calls[0]["logo"].get("options") or []
                box = options[-1]["logo_box"] if options else {"x": 100, "y": 200, "width": 176, "height": 176}
                groups.append({**group, "logo_box": box, "images": images})
            return AgentRunResult(run_id, "COMPLETED", json.dumps({"groups": groups}), list(reversed(artifacts)))

        def download_image(self, url):
            index = int(url.rsplit("-", 1)[1].split(".")[0])
            return _png(1080, 1350, (20 + index, 60, 90))

        def agent_artifact_redirect(self, url):
            raise AssertionError("Unique registered filenames need no redirect")

    client = SeriesClient()
    poster_series_round.run_generation(db, settings, store, client, launched[0])
    poster_series_round.run_generation(db, settings, store, client, launched[0])
    db.whole_series_runs.update_one({"_id": launched[0]}, {"$set": {"status": "running"}})
    poster_series_round.resume_runs(db, settings, store, client)
    assert len(client.calls) == 1
    assert db.whole_versions.count_documents({}) == 4
    full_input = client.calls[0]
    assert full_input["prompt"] == prompt and full_input["operation"] == "poster-series-generate"
    assert [(item["asset_id"], item["role"]) for item in full_input["references"]] == [
        ("asset-1", "content"), ("style-1", "style"), ("logo-1", "logo"),
    ]
    assert full_input["references"][-1]["url"].startswith("https://")
    options = full_input["logo"]["options"]
    assert len(options) == (1 if expected_position else 4)
    official_box = options[-1]["logo_box"]
    safe = options[-1]["safe_area"]
    assert safe["width"] <= official_box["width"] + 24
    assert safe["height"] <= official_box["height"] + 24
    if expected_position:
        assert options[0]["position"] == full_input["logo"]["placement"]["position"] == expected_position
    if expected_position == "top-left":
        assert official_box["x"] == official_box["y"] == 49
    if logo_position:
        assert official_box["x"] > 540 and official_box["y"] > 675
        assert full_input["logo"]["placement"]["size"] == "large"
        assert "takes precedence" in full_input["logo"]["instruction"]
    assert not ({"page_count", "cover", "pages"} & full_input.keys())
    snapshot = poster_series.get_workspace_series(db, "workspace-agent", settings)
    assert [group["page_count"] for group in snapshot["groups"]] == [1, 3]
    assert snapshot["runs"][0]["status"] == "complete"
    assert snapshot["runs"][0].get("logo_position") == logo_position
    for group in snapshot["groups"]:
        assert group["complete"]
        assert {key: group["logo_box"][key] for key in official_box} == official_box
        for order, page in enumerate(group["pages"]):
            assert page["role"] == "image" and page["order"] == order
            assert page["content_refs"] == [{"asset_id": "asset-1", "target": str(order)}]
            assert page["logo_box"] == group["logo_box"]
            version = db.whole_versions.find_one({"_id": page["version_id"]})
            assert [(item["asset_id"], item["role"]) for item in version["model_input"]["images"]] == [
                ("asset-1", "content"), ("style-1", "style_reference"), ("logo-1", "logo"),
            ]
            data, name = poster_series.page_download(db, store, group["id"], page["page_id"])
            assert data.startswith(b"\x89PNG") and name == f"{order + 1:02d}-图片.png"


# --------------------------------------------------------------------------- PS-08/09


def test_logo_safe_area_tracks_visible_mark_instead_of_transparent_padding():
    canvas = {"width": 1080, "height": 1440}
    artwork = {"image_size": [240, 240], "ink_bounds": [30, 70, 210, 170], "min_visible_px": 152}
    (option,) = poster_series._logo_options(canvas, "Logo 放左上角", artwork)
    box, safe = option["logo_box"], option["safe_area"]
    assert box["x"] == box["y"] == 49
    assert safe["height"] < box["height"]
    assert 152 <= safe["width"] < box["width"]
    assert safe["x"] > box["x"] and safe["y"] > box["y"]

def test_logo_box_and_composite_are_deterministic():
    canvas = {"width": 1080, "height": 1350}
    box = poster_series.resolve_logo_box(canvas, "方图，Logo 左下角小一点")
    assert box["position"] == "bottom-left" and box["size"] == "small"
    default = poster_series.resolve_logo_box(canvas, "甜品系列")
    assert default["position"] == "bottom-right"
    base = _png(1080, 1350, (250, 250, 250))
    logo = _logo_png()
    first = poster_series.composite_logo(base, logo, box, canvas)
    second = poster_series.composite_logo(base, logo, box, canvas)
    assert first == second
    with Image.open(io.BytesIO(first)) as image:
        assert image.size == (1080, 1350)
        # The logo's red pixels sit inside the requested bottom-left box.
        pixel = image.convert("RGB").getpixel((box["x"] + 40, box["y"] + 10))
        assert pixel[0] > 150 and pixel[1] < 100


def test_new_series_logo_rejects_an_unreadably_small_visible_mark():
    canvas = {"width": 1080, "height": 1440}
    box = {"x": 912, "y": 72, "width": 96, "height": 96, "min_visible_px": 152}
    base = _png(1080, 1440, (248, 227, 205))
    logo = _solid_mark_png((240, 240), (255, 255, 255, 255), inset=18)

    with pytest.raises(poster_series.SeriesError, match="Logo"):
        poster_series.composite_logo(base, logo, box, canvas)

    large_box = {**box, "x": 840, "width": 184, "height": 184}
    result = poster_series.composite_logo(base, logo, large_box, canvas)
    assert result != base
    with pytest.raises(poster_series.SeriesError, match="Logo"):
        poster_series.composite_logo(base, _png(200, 200, (255, 255, 255, 0), "RGBA"), large_box, canvas)


def test_low_contrast_light_logo_gets_a_dark_plate_without_moving_the_mark():
    canvas = {"width": 1080, "height": 1440}
    box = {"x": 360, "y": 1300, "width": 360, "height": 100}
    base = _png(1080, 1440, (243, 230, 215))
    logo = _solid_mark_png((800, 800), (252, 252, 252, 255), inset=80)
    first = poster_series.composite_logo(base, logo, box, canvas)
    assert first == poster_series.composite_logo(base, logo, box, canvas)
    scaled, left, top = _scaled_mark(box, logo)
    height = scaled.size[1]
    ink_x, ink_y = _opaque_ink_pixel(scaled)
    with Image.open(io.BytesIO(first)) as image:
        final = image.convert("RGB")
    # The official file keeps its own pixels at exactly the same placement.
    expected = scaled.getpixel((ink_x, ink_y))[:3]
    assert expected[0] > 240
    assert final.getpixel((left + ink_x, top + ink_y)) == expected
    # A neutral dark plate sits behind the mark and makes it readable.
    plate = final.getpixel((left + 6, top + height // 2))
    assert plate[0] < 90 and max(plate) - min(plate) < 25
    assert _contrast_ratio(expected, plate) >= 3.0
    # The design outside the Logo area is untouched.
    assert final.getpixel((5, 5)) == (243, 230, 215)
    assert final.getpixel((box["x"] - 5, box["y"] + 50)) == (243, 230, 215)
    assert final.getpixel((box["x"] + box["width"] + 5, box["y"] + 50)) == (243, 230, 215)


def test_low_contrast_dark_logo_gets_a_light_plate():
    canvas = {"width": 1080, "height": 1440}
    box = {"x": 360, "y": 1300, "width": 360, "height": 100}
    base = _png(1080, 1440, (40, 36, 34))
    logo = _solid_mark_png((800, 800), (30, 28, 27, 255), inset=80)
    scaled, left, top = _scaled_mark(box, logo)
    height = scaled.size[1]
    ink_x, ink_y = _opaque_ink_pixel(scaled)
    with Image.open(io.BytesIO(poster_series.composite_logo(base, logo, box, canvas))) as image:
        final = image.convert("RGB")
    assert final.getpixel((left + ink_x, top + ink_y)) == scaled.getpixel((ink_x, ink_y))[:3]
    plate = final.getpixel((left + 6, top + height // 2))
    assert plate[0] > 170 and max(plate) - min(plate) < 25
    assert _contrast_ratio((30, 28, 27), plate) >= 3.0
    assert final.getpixel((5, 5)) == (40, 36, 34)


def test_high_contrast_logo_keeps_the_plain_composite():
    canvas = {"width": 1080, "height": 1440}
    box = {"x": 360, "y": 1300, "width": 360, "height": 100}
    base = _png(1080, 1440, (250, 250, 250))
    logo = _solid_mark_png((800, 800), (200, 20, 20, 255), inset=80)
    scaled, left, top = _scaled_mark(box, logo)
    width, height = scaled.size
    with Image.open(io.BytesIO(poster_series.composite_logo(base, logo, box, canvas))) as image:
        final = image.convert("RGB")
    assert final.getpixel((left + 40, top + 40)) == scaled.getpixel((40, 40))[:3]
    # Nothing is painted between the mark and the box edge: no plate for a readable Logo.
    assert final.getpixel((left + 3, top + height // 2)) == (250, 250, 250)
    assert final.getpixel((left + width - 3, top + height // 2)) == (250, 250, 250)
    assert final.getpixel((box["x"], box["y"] + 2)) == (250, 250, 250)


def test_low_contrast_logo_plate_is_clipped_to_the_canvas():
    canvas = {"width": 200, "height": 240}
    box = {"x": 0, "y": 140, "width": 200, "height": 80}
    base = _png(200, 240, (245, 240, 232))
    logo = _solid_mark_png((400, 160), (252, 252, 252, 255), inset=4)
    with Image.open(io.BytesIO(poster_series.composite_logo(base, logo, box, canvas))) as image:
        assert image.size == (200, 240)
        final = image.convert("RGB")
    # The plate runs into the left canvas edge instead of overflowing it.
    assert final.getpixel((0, 180))[0] < 90
    assert final.getpixel((199, 235)) == (245, 240, 232)
    assert final.getpixel((199, 5)) == (245, 240, 232)


def test_logo_hints_only_apply_inside_logo_clauses_and_keep_other_property():
    canvas = {"width": 1080, "height": 1440}
    # "小红书" and "大标题" are content instructions, not Logo overrides.
    headline = poster_series.resolve_logo_box(canvas, "做小红书图文，右上角是标题，大标题放中间")
    assert headline["source"] == "default"
    assert headline["position"] == "bottom-right" and headline["size"] == "medium"
    # Position only: size stays unspecified for the Agent's choice.
    position = poster_series.resolve_logo_box(canvas, "Logo 放左下角")
    assert position["source"] == "user" and position["position"] == "bottom-left"
    assert position["position_specified"] is True and position["size_specified"] is False
    # Size only: placement stays unspecified.
    size = poster_series.resolve_logo_box(canvas, "Logo 小一点")
    assert size["size"] == "small" and size["size_specified"] is True
    assert size["position_specified"] is False
    agent_box = {
        "x": 40, "y": 30, "width": 300, "height": 90,
        "position": "top-left", "size": "agent", "fit": "contain", "source": "agent",
    }
    merged = poster_series.merge_logo_box(canvas, "Logo 放左下角", agent_box)
    assert merged["position"] == "bottom-left"
    assert merged["width"] == 300 and merged["height"] == 90
    assert merged["x"] < canvas["width"] // 2 and merged["y"] > canvas["height"] // 2
    merged_size = poster_series.merge_logo_box(canvas, "Logo 小一点", agent_box)
    assert merged_size["x"] == 40 and merged_size["y"] == 30
    assert merged_size["position"] == "top-left"
    assert merged_size["width"] < 300


def test_group_plan_integrates_logo_artwork_and_merges_user_box_hints(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    _asset(db, store, "asset-1", "内容图")
    _asset(db, store, "logo-1", "Logo")
    logo_asset = db.assets.find_one({"_id": "logo-1"})
    store.put_bytes(logo_asset["prepared_path"], _solid_mark_png((160, 80), (255, 255, 255, 255), inset=16), "image/png")
    payload = _payload(
        prompt="做小红书图文，右上角是标题；Logo 放左下角",
        reference_asset_ids=["asset-1"], logo_asset_id="logo-1",
    )
    result = _make_run(db, settings, store, payload)
    group = result["groups"][0]
    db_group = db.whole_series_groups.find_one({"_id": group["id"]})
    pages = list(db.whole_series_pages.find({"group_id": group["id"]}).sort("order", 1))
    client = FakeCoreAI()
    poster_series._ensure_group_plan(db, settings, store, client, db_group, pages)
    planned = db.whole_series_groups.find_one({"_id": group["id"]})
    plan_input = client.calls[0]["payload"]
    assert plan_input["group"]["logo"]["artwork"] == {"aspect_ratio": 2.0, "mark_color": "#FFFFFF"}
    assert {item["asset_id"] for item in plan_input["content_pool"]} == {"asset-1"}
    cover_task = db.whole_tasks.find_one({"_id": group["pages"][0]["whole_task_id"]})
    page_input = poster_series._agent_page_input(db, settings, store, client, cover_task, planned)
    assert page_input["group"]["logo"]["treatment"] == "Small cocoa-brown tab integrated into the title block"
    # The Agent's arbitrary box size is kept; only the stated position is applied.
    assert planned["logo_box"]["width"] == 300 and planned["logo_box"]["height"] == 90
    assert planned["logo_box"]["position"] == "bottom-left"
    assert planned["logo_box_source"] == "user"

    # Without a Logo clause the Agent's own box wins untouched.
    payload2 = _payload(
        submit_id="submit-no-logo-hint", prompt="做小红书图文，大标题放中间",
        reference_asset_ids=["asset-1"], logo_asset_id="logo-1",
    )
    result2 = _make_run(db, settings, store, payload2)
    group2 = result2["groups"][-1]
    db_group2 = db.whole_series_groups.find_one({"_id": group2["id"]})
    pages2 = list(db.whole_series_pages.find({"group_id": group2["id"]}).sort("order", 1))
    poster_series._ensure_group_plan(db, settings, store, FakeCoreAI(), db_group2, pages2)
    planned2 = db.whole_series_groups.find_one({"_id": group2["id"]})
    assert planned2["logo_box"]["x"] == 100 and planned2["logo_box"]["y"] == 200
    assert planned2["logo_box"]["position"] == "agent"
    assert planned2["logo_box_source"] == "agent"


def test_series_page_generation_composites_logo_and_keeps_base(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    _asset(db, store, "asset-1", "内容图")
    _asset(db, store, "logo-1", "官方 Logo")
    db.assets.update_one(
        {"_id": "logo-1"},
        {"$set": {"original_name": "logo.png", "path": "assets/logo-1.png",
                  "prepared_path": "assets/logo-1.png"}},
    )
    store.put_bytes("assets/logo-1.png", _logo_png(), "image/png")
    payload = _payload(prompt="Logo 左下角小一点", reference_asset_ids=["asset-1"],
                       logo_asset_id="logo-1")
    result = _make_run(db, settings, store, payload)
    group = result["groups"][0]
    task_id = group["pages"][0]["whole_task_id"]
    client = FakeCoreAI()
    poster_series.run_page_generation(db, settings, store, client, task_id)
    task = db.whole_tasks.find_one({"_id": task_id})
    version = db.whole_versions.find_one({"_id": task["current_version_id"]})
    assert task["status"] == "succeeded"
    assert version["series_base_asset_id"] != task["output_asset_id"]
    assert {output["id"] for output in version["outputs"]} == {"series_base", "series_final"}
    base_asset = db.assets.find_one({"_id": version["series_base_asset_id"]})
    final_asset = db.assets.find_one({"_id": task["output_asset_id"]})
    base_bytes = store.get_bytes(base_asset["path"])
    final_bytes = store.get_bytes(final_asset["path"])
    assert base_bytes != final_bytes
    box = group["logo_box"]
    with Image.open(io.BytesIO(base_bytes)) as base_image, Image.open(io.BytesIO(final_bytes)) as final_image:
        assert final_image.size == (1080, 1350)
        outside = [(5, 5), (1070, 5), (5, 1340),
                   (box["x"] + box["width"] + 10, box["y"]),
                   (box["x"], box["y"] - 10)]
        for point in outside:
            assert base_image.convert("RGB").getpixel(point) == final_image.convert("RGB").getpixel(point)
        inside = final_image.convert("RGB").getpixel((box["x"] + 40, box["y"] + 10))
        assert inside[0] > 150 and inside[1] < 100


def test_series_page_generation_without_logo_uses_single_output(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    _asset(db, store, "asset-1", "内容图")
    payload = _payload(reference_asset_ids=["asset-1"], logo_asset_id=None)
    result = _make_run(db, settings, store, payload)
    task_id = result["groups"][0]["pages"][0]["whole_task_id"]
    poster_series.run_page_generation(db, settings, store, FakeCoreAI(), task_id)
    task = db.whole_tasks.find_one({"_id": task_id})
    version = db.whole_versions.find_one({"_id": task["current_version_id"]})
    assert len(version["outputs"]) == 1
    assert version["outputs"][0]["selectable"] is True
    assert version["series_base_asset_id"] == task["output_asset_id"]


def test_trace_requires_all_expected_references(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    _asset(db, store, "asset-1", "内容")
    _asset(db, store, "asset-2", "封面")
    client = FakeCoreAI()
    client.last_trace = {
        "transcript": [{
            "tool": "generate_image",
            "arguments": {
                "model": "gpt-image-2.5-sunburst",
                "quality": "xhigh",
                "prompt": "p",
                "input_images": json.dumps([{"url": "https://core-ai.test/blob/1.png"}]),
            },
        }],
    }
    with pytest.raises(poster_series.SeriesError) as error:
        poster_series._verify_agent_trace(
            client, "run-1", "gpt-image-2.5-sunburst",
            ["https://core-ai.test/blob/1.png", "https://core-ai.test/blob/2.png"],
            "p",
        )
    assert error.value.code == "agent_trace_mismatch"


def test_resume_waits_saved_agent_run_without_new_trigger(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    _asset(db, store, "asset-1", "内容")
    result = _make_run(db, settings, store, _payload(reference_asset_ids=["asset-1"]))
    group = result["groups"][0]
    task_id = group["pages"][0]["whole_task_id"]
    task = db.whole_tasks.find_one({"_id": task_id})
    db_group = db.whole_series_groups.find_one({"_id": group["id"]})
    client = FakeCoreAI()
    pages = list(db.whole_series_pages.find({"group_id": group["id"]}).sort("order", 1))
    poster_series._ensure_group_plan(db, settings, store, client, db_group, pages)
    db_group = db.whole_series_groups.find_one({"_id": group["id"]})
    payload = poster_series._agent_page_input(db, settings, store, client, task, db_group)
    page_result = client._agent_result(json.dumps(payload, ensure_ascii=False))
    client.results["run-saved"] = page_result
    db.whole_tasks.update_one({"_id": task_id}, {"$set": {
        "series.agent_run_id": "run-saved",
        "series.status": "queued",
    }})
    client.trigger_count = 0
    poster_series.run_page_generation(db, settings, store, client, task_id)
    assert client.trigger_count == 0
    assert db.whole_tasks.find_one({"_id": task_id})["status"] == "succeeded"
    assert db.whole_versions.count_documents({"task_id": task_id}) == 1


# --------------------------------------------------------------------------- PS-10/11

def test_retry_reuses_page_task_and_skips_running(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    _asset(db, store, "asset-1", "封面图")
    payload = _payload(reference_asset_ids=["asset-1"])
    launched: list[str] = []
    result = _make_run(db, settings, store, payload, launched)
    group = result["groups"][0]
    db_group = db.whole_series_groups.find_one({"_id": group["id"]})
    version_id = _cover_version(db, store, db_group)
    poster_series.start_interiors(
        db, settings, group["id"], cover_version_id=version_id,
        launch=lambda task_id: launched.append(task_id), merchant_id=MERCHANT,
    )
    detail = db.whole_series_pages.find_one({"group_id": group["id"], "role": "detail"})
    task_id = detail["whole_task_id"]
    db.whole_tasks.update_one({"_id": task_id}, {"$set": {
        "status": "failed", "generation": {"status": "failed", "error": "boom"},
        "error": {"message": "boom"},
    }})
    before = task_id
    poster_series.retry_page(
        db, settings, group["id"], detail["_id"],
        launch=lambda value: launched.append(value), merchant_id=MERCHANT,
    )
    retried = db.whole_series_pages.find_one({"_id": detail["_id"]})
    assert retried["whole_task_id"] == before
    assert db.whole_tasks.find_one({"_id": task_id})["generation"]["status"] == "queued"
    db.whole_tasks.update_one({"_id": task_id}, {"$set": {"status": "running"}})
    count = len(launched)
    poster_series.retry_page(
        db, settings, group["id"], detail["_id"],
        launch=lambda value: launched.append(value), merchant_id=MERCHANT,
    )
    assert len(launched) == count


# --------------------------------------------------------------------------- PS-17

def _succeeded_series_page(db, store, *, page_id: str, group_id: str, content_ids: list[str],
                           version_id: str, image_model: str = "gpt-image-2.5-sunburst") -> dict:
    now = datetime.now(UTC)
    task_id = f"wtask_{uuid.uuid4().hex}"
    asset_id = f"asset_{uuid.uuid4().hex}"
    version_asset = f"asset_{uuid.uuid4().hex}"
    store.put_bytes(f"outputs/{asset_id}.png", _png(), "image/png")
    for output_asset in (asset_id, version_asset):
        store.put_bytes(f"outputs/{output_asset}.png", _png(), "image/png")
        db.assets.insert_one({
            "_id": output_asset, "kind": "output", "source": "generated",
            "original_name": f"{output_asset}.png", "path": f"outputs/{output_asset}.png",
            "prepared_path": f"outputs/{output_asset}.png", "mime_type": "image/png",
            "width": 120, "height": 120, "bytes": 10, "sha256": output_asset,
            "created_at": now,
        })
    output = {
        "id": "series_final", "channel": "gateway", "role": "series_final",
        "label": "成品", "asset_id": version_asset, "width": 120, "height": 120,
        "selectable": True, "model_input": {"images": []}, "created_at": now,
    }
    db.whole_versions.insert_one({
        "_id": version_id, "task_id": task_id, "version": 1,
        "parent_version_id": None, "output_asset_id": version_asset,
        "outputs": [output], "instruction": "page", "operation": "series_generate",
        "width": 120, "height": 120, "model_input": {"images": []},
        "series_content_refs": content_ids, "series_role": "detail",
        "created_at": now,
    })
    doc = {
        "_id": task_id, "status": "succeeded", "stage": "done", "progress": 100,
        "input": {"image_model": image_model, "generation_workspace_id": "workspace-1",
                  "logo_asset_id": "logo-1", "reference_asset_ids": content_ids},
        "brand": {"name": "SEEJOY", "logo_asset_ids": ["logo-1"]},
        "canvas": {"width": 120, "height": 120},
        "provider": {"name": "core-ai", "model": image_model, "status": "completed"},
        "generation": {"status": "completed", "error": None},
        "selected_version_id": version_id, "current_version_id": version_id,
        "current_version": 1, "current_output_id": "series_final",
        "output_asset_id": version_asset,
        "edit": {"status": "idle"},
        "series": {"workspace_id": "workspace-1", "group_id": group_id,
                   "page_id": page_id, "role": "detail", "content_refs": content_ids},
        "created_at": now, "updated_at": now,
    }
    db.whole_tasks.insert_one(doc)
    return doc


def test_series_finalization_counts_each_page_and_follows_version(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    _asset(db, store, "photo-1", "草莓杯")
    _asset(db, store, "photo-2", "千层")
    _asset(db, store, "logo-1", "官方 Logo")
    page_a = _succeeded_series_page(db, store, page_id="page-1", group_id="group-1",
                                    content_ids=["photo-1", "photo-1"], version_id="ver-a")
    page_b = _succeeded_series_page(db, store, page_id="page-2", group_id="group-1",
                                    content_ids=["photo-1"], version_id="ver-b")
    first = poster_finalizations.save(
        db, page_a["_id"], version_id="ver-a", output_id="series_final", expected_revision=None
    )
    second = poster_finalizations.save(
        db, page_b["_id"], version_id="ver-b", output_id="series_final", expected_revision=None
    )
    assert first["group_key"] != second["group_key"]
    assets = poster_finalizations.enrich_assets(db, [{"id": "photo-1"}])
    # The same original photo counts once per finalized page: 1 (duplicate on page A) + 1.
    assert assets[0]["usage_count"] == 2
    poster_finalizations.revoke(db, page_a["_id"], expected_revision=first["revision"])
    assert poster_finalizations.enrich_assets(db, [{"id": "photo-1"}])[0]["usage_count"] == 1
    # Logo and style references never enter the page's photo list.
    context = poster_finalizations.context(db, page_b["_id"], None, None)
    assert [item["id"] for item in context["assets"]] == ["photo-1"]


def test_replacement_version_counts_only_current_photo(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    _asset(db, store, "photo-2", "莓果杯")
    _asset(db, store, "photo-4", "替代莓果杯")
    _asset(db, store, "logo-1", "官方 Logo")
    page = _succeeded_series_page(
        db, store, page_id="page-1", group_id="group-1",
        content_ids=["photo-2"], version_id="ver-v1",
    )
    page["brand"]["logo_asset_ids"] = ["logo-1"]
    db.whole_tasks.replace_one({"_id": page["_id"]}, page)
    parent = db.whole_versions.find_one({"_id": "ver-v1"})
    parent["series_content_refs"] = [{"asset_id": "photo-2", "target": "莓果杯"}]
    db.whole_versions.replace_one({"_id": "ver-v1"}, parent)
    now = datetime.now(UTC)
    asset_v2 = "asset_v2_output"
    store.put_bytes(f"outputs/{asset_v2}.png", _png(), "image/png")
    db.assets.insert_one({
        "_id": asset_v2, "kind": "output", "source": "generated",
        "original_name": f"{asset_v2}.png", "path": f"outputs/{asset_v2}.png",
        "prepared_path": f"outputs/{asset_v2}.png", "mime_type": "image/png",
        "width": 120, "height": 120, "bytes": 10, "sha256": asset_v2,
        "created_at": now,
    })
    v2 = {
        "_id": "ver-v2", "task_id": page["_id"], "version": 2,
        "parent_version_id": "ver-v1", "output_asset_id": asset_v2,
        "outputs": [{
            "id": "series_final", "channel": "agent", "role": "series_final",
            "label": "成品", "asset_id": asset_v2, "width": 120, "height": 120,
            "selectable": True, "model_input": {"images": []}, "created_at": now,
        }],
        "instruction": "replace", "operation": "original_replacement",
        "width": 120, "height": 120, "model_input": {"images": []},
        "replacements": [{"target": "莓果杯", "asset_id": "photo-4"}],
        "created_at": now,
    }
    poster_series.attach_series_refs(db, page["_id"], v2)
    assert [item["asset_id"] for item in v2["series_content_refs"]] == ["photo-4"]
    db.whole_versions.insert_one(v2)
    db.whole_tasks.update_one({"_id": page["_id"]}, {"$set": {
        "current_version_id": "ver-v2", "selected_version_id": "ver-v2",
        "current_version": 2, "current_output_id": "series_final",
        "output_asset_id": asset_v2,
    }})
    saved = poster_finalizations.save(
        db, page["_id"], version_id="ver-v2", output_id="series_final",
        expected_revision=None,
    )
    context = poster_finalizations.context(db, page["_id"], "ver-v2", "series_final")
    assert [item["id"] for item in context["assets"]] == ["photo-4"]
    # Switching back to v1 restores the original association.
    context_v1 = poster_finalizations.context(db, page["_id"], "ver-v1", "series_final")
    assert [item["id"] for item in context_v1["assets"]] == ["photo-2"]
    poster_finalizations.revoke(db, page["_id"], expected_revision=saved["revision"])


def test_flag_off_hides_series_tasks_from_single_lists(tmp_path):
    from app.routes import whole_image as routes

    settings = _settings(tmp_path, poster_series_enabled=False)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    _asset(db, store, "asset-1", "图")
    single = _build_task(WholeImageTaskCreate(
        prompt="单张", workspace_title=None,
        generation_workspace_id="workspace-single",
        reference_asset_ids=["asset-1"],
    ))
    single["status"] = "succeeded"
    db.whole_tasks.insert_one(single)
    series = _build_task(WholeImageTaskCreate(
        prompt="系列页", generation_workspace_id="workspace-series",
        reference_asset_ids=["asset-1"],
    ))
    series["series"] = {"run_id": "r", "group_id": "g", "page_id": "p", "role": "cover"}
    db.whole_tasks.insert_one(series)
    groups = routes._list_task_groups(db, exclude_series=True)
    assert {group["id"] for group in groups} == {"workspace-single"}


# --------------------------------------------------------------------------- PS-18

def test_group_zip_order_and_incomplete_guard(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    _asset(db, store, "photo-1", "照片")
    now = datetime.now(UTC)
    db.whole_series_groups.insert_one({
        "_id": "group-1", "run_id": "run-1", "workspace_id": "w", "order": 0,
        "image_model": "gpt-image-2.5-sunburst", "page_count": 2,
        "created_at": now, "updated_at": now,
    })
    for order, role in enumerate(["cover", "detail"]):
        page_id = f"page-{order}"
        task = _succeeded_series_page(
            db, store, page_id=page_id, group_id="group-1",
            content_ids=["photo-1"], version_id=f"ver-{order}",
        )
        db.whole_series_pages.insert_one({
            "_id": page_id, "run_id": "run-1", "group_id": "group-1", "workspace_id": "w",
            "order": order, "role": role, "status": "succeeded",
            "whole_task_id": task["_id"], "content_refs": [], "style_reference_ids": [],
            "cover_reference": None, "logo_box": None, "created_at": now, "updated_at": now,
        })
    data, filename = poster_series.group_download(db, store, "group-1")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist() == ["01-封面.png", "02-介绍页.png"]
        assert archive.read("01-封面.png")
    assert filename.startswith("poster-series-")
    db.whole_series_pages.update_one({"_id": "page-1"}, {"$set": {"status": "failed"}})
    with pytest.raises(poster_series.SeriesError) as error:
        poster_series.group_download(db, store, "group-1")
    assert error.value.code == "group_incomplete"


# --------------------------------------------------------------------------- PS-19

def test_series_records_are_merchant_scoped(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    raw = mongomock.MongoClient().db
    db_a = ScopedDatabase(raw, "merchant-a")
    _asset(db_a, store, "asset-1", "图")
    result = _make_run(db_a, settings, store, _payload(reference_asset_ids=["asset-1"]))
    group_id = result["groups"][0]["id"]
    db_b = ScopedDatabase(raw, "merchant-b")
    with pytest.raises(poster_series.SeriesError) as error:
        poster_series._group(db_b, group_id)
    assert error.value.code == "group_not_found"
    assert poster_series.get_workspace_series(db_b, "workspace-1", settings)["groups"] == []


# --------------------------------------------------------------------------- mode rollback / cover lock

def test_invalid_submission_does_not_lock_workspace_mode(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    _asset(db, store, "asset-1", "图")

    def _bad_build(payload):
        raise RuntimeError("invalid brand")

    with pytest.raises(RuntimeError):
        poster_series.create_run(
            db, settings, "workspace-bad", _payload(reference_asset_ids=["asset-1"]),
            build_task=_bad_build, launch=lambda task_id: None,
        )
    assert db.whole_workspace_modes.find_one({"workspace_id": "workspace-bad"}) is None
    assert db.whole_series_runs.find_one({"workspace_id": "workspace-bad"}) is None
    # A corrected retry is accepted by the same workspace.
    result = _make_run(db, settings, store, _payload(reference_asset_ids=["asset-1"]))
    assert result["mode"] == "series"


def test_interiors_lock_one_cover_version_for_all_pages(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    _asset(db, store, "asset-1", "图")
    result = _make_run(db, settings, store, _payload(prompt="做 3 张", pages_per_group=3,
                                                    reference_asset_ids=["asset-1"]))
    group = result["groups"][0]
    db_group = db.whole_series_groups.find_one({"_id": group["id"]})
    _asset(db, store, "cover-base", "封面底图")
    v2 = _cover_version(db, store, db_group, base_asset="cover-base")
    # A second, competing selection arrives after the lock. It must not move any page.
    first = poster_series.start_interiors(
        db, settings, group["id"], cover_version_id=v2,
        launch=lambda task_id: None, merchant_id=MERCHANT,
    )
    db_group = db.whole_series_groups.find_one({"_id": group["id"]})
    db_group["interiors_started_at"] = None
    db.whole_series_groups.replace_one({"_id": group["id"]}, db_group)
    other = db.whole_versions.find_one({"_id": v2})
    other_id = "wver_other"
    db.whole_versions.insert_one({**other, "_id": other_id, "version": 2,
                                  "series_base_asset_id": "asset-other"})
    second = poster_series.start_interiors(
        db, settings, group["id"], cover_version_id=other_id,
        launch=lambda task_id: None, merchant_id=MERCHANT,
    )
    details = [page for page in second["pages"] if page["role"] == "detail"]
    assert all(page["cover_reference"]["version_id"] == v2 for page in details)
    assert db.whole_series_groups.find_one({"_id": group["id"]})["cover_lock"]["version_id"] == v2


# --------------------------------------------------------------------------- routing / flag

def _request(db, settings, store, client=None):
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            db=db, settings=settings, store=store, core_ai=client or FakeCoreAI(),
        )),
        state=SimpleNamespace(db=db, store=store, merchant_id=MERCHANT, merchant_profile=None),
        path_params={"merchant_id": MERCHANT},
    )
    return request


def test_series_routes_are_hidden_when_flag_off(tmp_path):
    from app.routes import poster_series as routes

    settings = _settings(tmp_path, poster_series_enabled=False)
    db = mongomock.MongoClient().db
    store = LocalObjectStore(settings.storage_path / "objects")
    request = _request(db, settings, store)
    assert routes.series_config(request) == {"enabled": False}
    with pytest.raises(HTTPException) as error:
        routes.list_series_groups("workspace-1", request)
    assert error.value.status_code == 404
    assert poster_series.get_workspace_mode(db, "workspace-1", settings)["series_enabled"] is False


def test_create_series_route_returns_groups(tmp_path):
    from app.routes import poster_series as routes

    settings = _settings(tmp_path)
    db = mongomock.MongoClient().db
    store = LocalObjectStore(settings.storage_path / "objects")
    _asset(db, store, "asset-1", "图")
    request = _request(db, settings, store, FakeCoreAI())
    payload = _payload(reference_asset_ids=["asset-1"])
    background = BackgroundTasks()
    result = routes.create_series_run("workspace-1", payload, request, background)
    assert result["mode"] == "series"
    assert result["groups"][0]["page_count"] == 0
    assert result["groups"][0]["pages"] == []
    assert len(background.tasks) == 1 and background.tasks[0].func is poster_series_round.run_generation


def test_create_series_route_checks_canvas_for_every_model_before_queuing(tmp_path):
    from app.routes import poster_series as routes

    settings = _settings(tmp_path)
    db = mongomock.MongoClient().db
    store = LocalObjectStore(settings.storage_path / "objects")
    _asset(db, store, "asset-1", "图")
    request = _request(db, settings, store)
    background = BackgroundTasks()
    payload = _payload(
        platform_id="instagram", placement_id="portrait", reference_asset_ids=["asset-1"],
        image_groups=[
            SeriesModelGroup(image_model="minimax-h3-image-edit"),
            SeriesModelGroup(image_model="seedream-5.5-image-to-image"),
        ],
    )

    with pytest.raises(HTTPException) as error:
        routes.create_series_run("workspace-1", payload, request, background)

    assert error.value.status_code == 422
    assert "不支持 4:5" in error.value.detail
    assert db.whole_workspace_modes.count_documents({}) == 0
    assert db.whole_series_runs.count_documents({}) == 0
    assert db.whole_series_groups.count_documents({}) == 0
    assert not background.tasks


def test_create_series_route_leaves_image_count_to_the_agent(tmp_path):
    from app.routes import poster_series as routes

    settings = _settings(tmp_path, poster_series_max_pages=6)
    db = mongomock.MongoClient().db
    store = LocalObjectStore(settings.storage_path / "objects")
    _asset(db, store, "asset-1", "图")
    request = _request(db, settings, store, FakeCoreAI())
    payload = _payload(prompt="每组 9 张", reference_asset_ids=["asset-1"])
    result = routes.create_series_run("workspace-agent", payload, request, BackgroundTasks())
    assert result["runs"][0]["prompt"] == "每组 9 张"
    assert result["runs"][0]["pages_per_group"] is None
    assert result["groups"][0]["page_count"] == 0


def test_series_platform_canvas_wins_when_ratio_is_omitted(tmp_path):
    from app.routes import poster_series as routes

    settings = _settings(tmp_path)
    db = mongomock.MongoClient().db
    store = LocalObjectStore(settings.storage_path / "objects")
    _asset(db, store, "asset-1", "图")
    request = _request(db, settings, store, FakeCoreAI())
    # xiaohongshu/note is 3:4 1080x1440. Omitting ratio must not fall back to 9:16.
    payload = _payload(
        reference_asset_ids=["asset-1"], platform_id="xiaohongshu", placement_id="note",
    )
    assert payload.ratio is None
    result = routes.create_series_run("workspace-2", payload, request, BackgroundTasks())
    group = result["groups"][0]
    assert group["canvas"] == {"width": 1080, "height": 1440}
    task = db.whole_series_groups.find_one({"_id": group["id"]})["task_template"]
    assert task["input"]["ratio"] == "3:4"
    assert task["canvas"] == {"width": 1080, "height": 1440}
    assert task["input"]["platform_id"] == "xiaohongshu"
    assert task["input"]["placement_id"] == "note"
    # An explicit ratio that matches the placement still works and stays on the platform
    # canvas; the platform rejects a mismatching explicit ratio (422) rather than the
    # series layer silently overriding it.
    explicit = _payload(
        submit_id="submit-explicit-ratio", reference_asset_ids=["asset-1"],
        platform_id="xiaohongshu", placement_id="note", ratio="3:4",
    )
    explicit_result = routes.create_series_run("workspace-3", explicit, request, BackgroundTasks())
    assert explicit_result["groups"][0]["canvas"] == {"width": 1080, "height": 1440}


def test_group_exposes_uploaded_content_pool_without_style_or_logo(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    for index in range(1, 6):
        _asset(db, store, f"asset-{index}", f"图{index}")
    _asset(db, store, "style-1", "风格")
    _asset(db, store, "logo-1", "Logo")
    payload = _payload(
        prompt="用 @图1 做封面",
        reference_asset_ids=[f"asset-{index}" for index in range(1, 6)],
        style_reference_asset_ids=["style-1"], logo_asset_id="logo-1",
    )
    result = _make_run(db, settings, store, payload)
    group = result["groups"][0]
    pool = group["content_pool"]
    assert [item["asset_id"] for item in pool] == [f"asset-{index}" for index in range(1, 6)]
    assert pool[0]["name"] == "图1"
    assert pool[0]["preview_url"] and pool[0]["thumb_url"]
    assert group["style_reference_ids"] == ["style-1"]
    assert all(item["asset_id"] not in {"style-1", "logo-1"} for item in pool)


# --------------------------------------------------------------------------- PS-09/15/16

def _generated_series_page(tmp_path, *, logo=True, prompt="系列海报", reference="asset-1"):
    """Generate one real series page through the fake Agent and return its documents."""
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    _asset(db, store, reference, "内容图")
    logo_id = None
    if logo:
        logo_id = "logo-1"
        _asset(db, store, logo_id, "官方 Logo")
        db.assets.update_one({"_id": logo_id}, {"$set": {"path": "assets/logo-1.png",
                                                        "prepared_path": "assets/logo-1.png"}})
        store.put_bytes("assets/logo-1.png", _logo_png(), "image/png")
    payload = _payload(prompt=prompt, reference_asset_ids=[reference], logo_asset_id=logo_id)
    result = _make_run(db, settings, store, payload)
    group = result["groups"][0]
    task_id = group["pages"][0]["whole_task_id"]
    client = FakeCoreAI()
    poster_series.run_page_generation(db, settings, store, client, task_id)
    task = db.whole_tasks.find_one({"_id": task_id})
    version = db.whole_versions.find_one({"_id": task["current_version_id"]})
    return settings, store, db, client, group, task, version


def _assert_logo_on_final(store, db, version, task, canvas, box):
    base_bytes = store.get_bytes(db.assets.find_one({"_id": version["series_base_asset_id"]})["path"])
    final_bytes = store.get_bytes(db.assets.find_one({"_id": task["output_asset_id"]})["path"])
    assert base_bytes != final_bytes
    with Image.open(io.BytesIO(base_bytes)) as base_image, Image.open(io.BytesIO(final_bytes)) as final_image:
        assert final_image.size == (canvas["width"], canvas["height"])
        for point in [(5, 5), (canvas["width"] - 10, 5),
                      (box["x"] + box["width"] + 10, box["y"])]:
            assert base_image.convert("RGB").getpixel(point) == final_image.convert("RGB").getpixel(point)
        base_rgb = base_image.convert("RGB")
        final_rgb = final_image.convert("RGB")
        logo_pixels = [
            (x, y) for x in range(box["x"], min(canvas["width"], box["x"] + box["width"]), 4)
            for y in range(box["y"], min(canvas["height"], box["y"] + box["height"]), 4)
            if final_rgb.getpixel((x, y)) != base_rgb.getpixel((x, y))
            and final_rgb.getpixel((x, y))[0] > 150 and final_rgb.getpixel((x, y))[1] < 100
        ]
        assert logo_pixels, "official Logo pixels were not composited inside the stored box"


def test_series_edit_edits_base_and_reapplies_logo(tmp_path):
    from app.services import whole_image

    settings, store, db, client, group, task, source = _generated_series_page(tmp_path)
    canvas = group["canvas"]
    base_path = db.assets.find_one({"_id": source["series_base_asset_id"]})["path"]
    db.whole_tasks.update_one({"_id": task["_id"]}, {"$set": {
        "edit": {"status": "running", "image_model": "gpt-image-2.5-sunburst",
                 "source_version_id": task["current_version_id"],
                 "source_output_id": "series_final", "reference_asset_ids": []},
    }})
    whole_image.run_edit(
        db, settings, store, client, task["_id"], "标题改大",
        task["current_version_id"], "series_final",
    )
    updated = db.whole_tasks.find_one({"_id": task["_id"]})
    edited = db.whole_versions.find_one({"_id": updated["current_version_id"]})
    assert edited["operation"] == "edit"
    assert edited["parent_version_id"] == source["_id"]
    # The gateway received the no-Logo base, never the composited deliverable.
    sent_paths = {str(ref.path) for ref in client.edit_calls[0]["refs"] if ref.path is not None}
    assert any(path.endswith(base_path.split("/")[-1]) for path in sent_paths)
    assert {output["role"] for output in edited["outputs"]} == {"gateway_result", "series_final"}
    _assert_logo_on_final(store, db, edited, updated, canvas, edited["series_logo_box"])
    # The content reference list follows the edited version, not the Logo or the cover.
    assert [item["asset_id"] for item in edited["series_content_refs"]] == ["asset-1"]
    assert edited["series_content_refs"][0]["target"]


def test_series_replacement_apply_keeps_logo_and_tracks_new_photo(tmp_path):
    from app.services import original_replacement

    settings, store, db, client, group, task, source = _generated_series_page(tmp_path)
    _asset(db, store, "asset-2", "替换图", color=(20, 120, 220))
    base_asset_id = source["series_base_asset_id"]
    now = datetime.now(UTC)
    result_asset = "asset_replacement_result"
    store.put_bytes(f"outputs/{result_asset}.png", _png(1080, 1350), "image/png")
    db.assets.insert_one({
        "_id": result_asset, "kind": "output", "source": "generated",
        "original_name": "replace.png", "path": f"outputs/{result_asset}.png",
        "prepared_path": f"outputs/{result_asset}.png", "mime_type": "image/png",
        "width": 120, "height": 120, "bytes": 10, "sha256": result_asset,
        "created_at": now,
    })
    db.background_replace_tasks.insert_one({
        "_id": "brep_test", "status": "succeeded", "stage": "done",
        "whole_task_id": task["_id"], "kind": "agent",
        "source_version_id": source["_id"], "source_output_id": "series_final",
        "source": {"asset_id": base_asset_id, "name": "底图", "width": 1080, "height": 1350},
        "input": {"source_asset_id": base_asset_id,
                  "replacements": [{"target": "内容", "asset_id": "asset-2"}],
                  "split_all_elements": False},
        "replacements": [{"target": "内容", "asset_id": "asset-2", "name": "替换图",
                          "width": 120, "height": 120}],
        "logo_reference": {"asset_id": group["logo_asset_id"], "name": "官方 Logo"},
        "output": {"asset_id": result_asset, "width": 1080, "height": 1350},
        "artifacts": [],
    })
    doc = original_replacement.apply(
        db, task["_id"], "brep_test",
        expected_current_version_id=source["_id"],
        expected_current_output_id="series_final",
        settings=settings, store=store,
    )
    applied = db.whole_versions.find_one({"_id": doc["current_version_id"]})
    assert applied["operation"] == "original_replace"
    assert applied["model_input"]["images"][-1]["asset_id"] == group["logo_asset_id"]
    assert applied["model_input"]["images"][-1]["role"] == "官方 Logo 参考"
    assert applied["series_content_refs"] == [{"asset_id": "asset-2", "target": "内容"}]
    # v1 still belongs to the original photo with its original target text.
    assert [item["asset_id"] for item in db.whole_versions.find_one(
        {"_id": source["_id"]})["series_content_refs"]] == ["asset-1"]
    _assert_logo_on_final(store, db, applied, doc, group["canvas"], applied["series_logo_box"])


def _html_editor_context(
    db,
    task,
    source,
    *,
    replacements,
    artifact_id="wart_html",
    kind="html",
    source_version_id=None,
    source_output_id="series_final",
    replacement_id="brep_html",
):
    """Create the replacement record plus its stored HTML artifact for one page."""
    db.background_replace_tasks.insert_one({
        "_id": replacement_id,
        "status": "succeeded",
        "stage": "done",
        "whole_task_id": task["_id"],
        "kind": "agent",
        "source_version_id": source_version_id or source["_id"],
        "source_output_id": source_output_id,
        "input": {"replacements": replacements, "source_asset_id": "", "split_all_elements": False},
        "replacements": [],
        "output": None,
        "artifacts": [],
    })
    db.whole_artifacts.insert_one({
        "_id": artifact_id,
        "task_id": replacement_id,
        "kind": kind,
        "content_type": "text/html" if kind == "html" else "image/png",
        "file_name": "editor.html",
        "path": "artifacts/editor.html",
    })
    return replacement_id


def _html_payload(
    task,
    group,
    *,
    png=None,
    source_version_id=None,
    refs=None,
    session="session-abcdef12",
    artifact_id="wart_html",
    expected_output_id="series_final",
):
    import base64

    from app.schemas import SeriesHtmlSaveRequest

    png = png or _png(group["canvas"]["width"], group["canvas"]["height"], (90, 140, 60))
    return SeriesHtmlSaveRequest(
        source_version_id=source_version_id or task["current_version_id"],
        expected_output_id=expected_output_id,
        artifact_id=artifact_id,
        session_id=session,
        png_base64=base64.b64encode(png).decode(),
        width=group["canvas"]["width"],
        height=group["canvas"]["height"],
        content_refs=refs if refs is not None else [{"asset_id": "asset-1", "target": "内容"}],
    )


def test_html_save_creates_one_logo_version_and_is_idempotent(tmp_path):
    settings, store, db, client, group, task, source = _generated_series_page(tmp_path)
    _asset(db, store, "asset-2", "新图", color=(30, 200, 90))
    _html_editor_context(db, task, source, replacements=[
        {"target": "内容", "asset_id": "asset-2"},
    ])
    payload = _html_payload(task, group, refs=[{"asset_id": "asset-2", "target": "内容"}])
    page = poster_series.html_save(
        db, settings, store, group["id"], group["pages"][0]["page_id"], payload, merchant_id=MERCHANT
    )
    assert page["version"] == 2
    assert page["content_refs"] == [{"asset_id": "asset-2", "target": "内容"}]
    saved = db.whole_versions.find_one({"_id": page["version_id"]})
    assert saved["operation"] == "series_html_save"
    assert saved["parent_version_id"] == source["_id"]
    assert saved["model_input"]["parameters"]["session_id"] == "session-abcdef12"
    assert saved["model_input"]["parameters"]["png_sha256"]
    assert saved["series_html_save"]["replacement_id"] == "brep_html"
    _assert_logo_on_final(store, db, saved, db.whole_tasks.find_one({"_id": task["_id"]}),
                          group["canvas"], saved["series_logo_box"])
    # The page lock is released and the task still accepts its own result.
    assert "series.html_save_token" not in db.whole_tasks.find_one({"_id": task["_id"]})
    # The same editor payload is retried: still exactly one extra version.
    again = poster_series.html_save(
        db, settings, store, group["id"], group["pages"][0]["page_id"], payload, merchant_id=MERCHANT
    )
    assert again["version_id"] == page["version_id"]
    assert db.whole_versions.count_documents({"task_id": task["_id"]}) == 2


def test_html_save_derives_new_photo_from_replacement_not_old_defaults(tmp_path):
    settings, store, db, client, group, task, source = _generated_series_page(tmp_path)
    _asset(db, store, "asset-4", "莓果杯", color=(160, 40, 200))
    # The page v1 uses @图1; the operator replaces that position with @图4.
    _html_editor_context(db, task, source, replacements=[
        {"target": "内容图", "asset_id": "asset-4"},
    ], artifact_id="wart_html_swap", replacement_id="brep_swap")
    # The browser still sends the page's old defaults (@图1).
    payload = _html_payload(
        task, group, artifact_id="wart_html_swap",
        refs=[{"asset_id": "asset-1", "target": "内容图"}],
        session="session-swap-0001",
    )
    page = poster_series.html_save(
        db, settings, store, group["id"], group["pages"][0]["page_id"], payload, merchant_id=MERCHANT
    )
    assert [item["asset_id"] for item in page["content_refs"]] == ["asset-4"]
    saved = db.whole_versions.find_one({"_id": page["version_id"]})
    assert [item["asset_id"] for item in saved["series_content_refs"]] == ["asset-4"]
    # v1 keeps its own photo association.
    assert [item["asset_id"] for item in db.whole_versions.find_one(
        {"_id": source["_id"]})["series_content_refs"]] == ["asset-1"]
    # Finalization reads the new version's photo, not the replaced one.
    assert [item["asset_id"] for item in poster_series._current_content_refs(
        db, task, saved, group["pages"][0])] == ["asset-4"]


def test_html_save_rejects_stale_bad_png_and_foreign_artifact(tmp_path):
    settings, store, db, client, group, task, source = _generated_series_page(tmp_path)
    page_id = group["pages"][0]["page_id"]
    _html_editor_context(db, task, source, replacements=[
        {"target": "内容图", "asset_id": "asset-1"},
    ])
    bad_size = _html_payload(task, group, png=_png(120, 120))
    with pytest.raises(poster_series.SeriesError) as error:
        poster_series.html_save(db, settings, store, group["id"], page_id, bad_size)
    assert error.value.status_code == 422 and error.value.code == "invalid_canvas_size"
    broken = _html_payload(task, group)
    broken.png_base64 = "bm90IGEgcG5n"
    with pytest.raises(poster_series.SeriesError) as error:
        poster_series.html_save(db, settings, store, group["id"], page_id, broken)
    assert error.value.status_code == 422
    # Artifacts from another page are rejected before a version is written.
    db.whole_artifacts.insert_one({"_id": "wart_foreign", "task_id": "another-task", "kind": "html"})
    foreign = _html_payload(task, group, artifact_id="wart_foreign")
    with pytest.raises(poster_series.SeriesError) as error:
        poster_series.html_save(db, settings, store, group["id"], page_id, foreign)
    assert error.value.status_code == 409
    # A non-HTML artifact is not an editor result.
    _html_editor_context(db, task, source, replacements=[], artifact_id="wart_png",
                         kind="image", replacement_id="brep_png")
    png_artifact = _html_payload(task, group, artifact_id="wart_png")
    with pytest.raises(poster_series.SeriesError) as error:
        poster_series.html_save(db, settings, store, group["id"], page_id, png_artifact)
    assert error.value.code == "artifact_not_html"
    # Missing artifact is rejected by the service contract too.
    missing = _html_payload(task, group, artifact_id="wart_missing")
    with pytest.raises(poster_series.SeriesError) as error:
        poster_series.html_save(db, settings, store, group["id"], page_id, missing)
    assert error.value.code == "artifact_not_found"
    assert db.whole_versions.count_documents({"task_id": task["_id"]}) == 1
    assert db.whole_series_html_saves.count_documents({}) == 0


def test_html_save_rejects_stale_or_concurrent_sessions(tmp_path):
    settings, store, db, client, group, task, source = _generated_series_page(tmp_path)
    page_id = group["pages"][0]["page_id"]
    _html_editor_context(db, task, source, replacements=[
        {"target": "内容图", "asset_id": "asset-1"},
    ], artifact_id="wart_a", replacement_id="brep_a")
    _asset(db, store, "asset-3", "另一张", color=(10, 90, 160))
    _html_editor_context(db, task, source, replacements=[
        {"target": "内容图", "asset_id": "asset-3"},
    ], artifact_id="wart_b", replacement_id="brep_b")
    first = _html_payload(task, group, artifact_id="wart_a", session="session-first-1",
                          png=_png(group["canvas"]["width"], group["canvas"]["height"], (10, 10, 10)))
    page = poster_series.html_save(db, settings, store, group["id"], page_id, first)
    # A second editor opened from v1 and publishing later must not overwrite v2.
    second = _html_payload(task, group, artifact_id="wart_b", session="session-second-2",
                           png=_png(group["canvas"]["width"], group["canvas"]["height"], (200, 10, 10)))
    with pytest.raises(poster_series.SeriesError) as error:
        poster_series.html_save(db, settings, store, group["id"], page_id, second)
    assert error.value.status_code == 409
    assert db.whole_versions.count_documents({"task_id": task["_id"]}) == 2
    # A lock held by another publisher makes a claim fail instead of queueing behind it.
    other_task = db.whole_tasks.find_one({"_id": task["_id"]})
    db.whole_tasks.update_one({"_id": task["_id"]}, {
        "$set": {"edit.status": "running", "series.html_save_token": "other-token"},
    })
    waiting = _html_payload(
        other_task, group, artifact_id="wart_a", session="session-third-3",
        source_version_id=page["version_id"],
        png=_png(group["canvas"]["width"], group["canvas"]["height"], (0, 40, 0)),
    )
    _html_editor_context(db, task, {"_id": page["version_id"]}, replacements=[],
                         artifact_id="wart_c", replacement_id="brep_c")
    waiting.artifact_id = "wart_c"
    with pytest.raises(poster_series.SeriesError) as error:
        poster_series.html_save(db, settings, store, group["id"], page_id, waiting)
    assert error.value.status_code == 409
    assert db.whole_versions.count_documents({"task_id": task["_id"]}) == 2


def test_html_save_route_is_hidden_when_flag_off(tmp_path):
    from app.routes import poster_series as routes
    from app.schemas import SeriesHtmlSaveRequest

    settings = _settings(tmp_path, poster_series_enabled=False)
    db = mongomock.MongoClient().db
    store = LocalObjectStore(settings.storage_path / "objects")
    request = _request(db, settings, store)
    payload = SeriesHtmlSaveRequest(
        source_version_id="v1", expected_output_id="series_final", artifact_id="wart_x",
        session_id="session-abcdef12", png_base64="aGVsbG8=", width=10, height=10,
        content_refs=[],
    )
    with pytest.raises(HTTPException) as error:
        routes.save_series_html_page("group", "page", payload, request)
    assert error.value.status_code == 404


def test_trace_accepts_successful_native_span_when_args_are_absent():
    client = FakeCoreAI()
    client.last_trace = {
        "trace_id": "trace-1",
        "transcript": [{"tool": "generate_image", "name": None, "arguments": None}],
    }
    client.spans = [{
        "id": "span-1", "name": "generate_image", "status": "OK",
        "attributes": {"tool.name": "generate_image", "gen_ai.operation.name": "execute_tool"},
    }]
    calls, accepted = poster_series._verify_agent_trace(
        client, "run-1", "gpt-image-2.5-sunburst",
        ["https://core-ai.test/blob/1.png"], "agent prompt",
    )
    assert calls == []
    verification = accepted["verification"]
    assert verification["level"] == "tool_span_reported"
    assert verification["trace_id"] == "trace-1" and verification["span_id"] == "span-1"
    assert verification["caveat"]
    # A structured mismatch is still rejected even when a span exists.
    client.last_trace = {
        "trace_id": "trace-1",
        "transcript": [{
            "tool": "generate_image",
            "arguments": {"model": "gpt-image-2.5-sunburst", "quality": "xhigh",
                          "prompt": "agent prompt",
                          "input_images": json.dumps([{"url": "https://other.test/1.png"}])},
        }],
    }
    with pytest.raises(poster_series.SeriesError) as error:
        poster_series._verify_agent_trace(
            client, "run-1", "gpt-image-2.5-sunburst",
            ["https://core-ai.test/blob/1.png"], "agent prompt",
        )
    assert error.value.code == "agent_trace_mismatch"


def test_trace_absent_arguments_and_spans_still_fails():
    client = FakeCoreAI()
    client.last_trace = {"trace_id": "trace-2", "transcript": []}
    client.spans = []
    with pytest.raises(poster_series.SeriesError) as error:
        poster_series._verify_agent_trace(
            client, "run-2", "gpt-image-2.5-sunburst",
            ["https://core-ai.test/blob/1.png"], "agent prompt",
        )
    assert error.value.code == "agent_trace_missing"
    assert "generate_image" in error.value.message


# --------------------------------------------------------------------------- PS-02 limit

def test_page_count_over_limit_reports_the_limit_instead_of_clamping(tmp_path):
    settings = _settings(tmp_path, poster_series_max_pages=6)
    assert poster_series.resolve_page_count("做 6 张", settings=settings) == 6
    for text, explicit in (("做 7 张", None), ("做 2 张内页", 9), ("共 20 张", None)):
        with pytest.raises(poster_series.SeriesError) as error:
            poster_series.resolve_page_count(text, explicit, settings)
        assert error.value.code == "pages_per_group_limit"
        assert error.value.status_code == 422
        assert "2–6" in error.value.message and "页" in error.value.message
    # A number that only describes content is still not a page count.
    assert poster_series.resolve_page_count("介绍 20 种甜品", settings=settings) == 2
    # The minimum is a cover plus one interior; below that the same error explains it.
    with pytest.raises(poster_series.SeriesError) as error:
        poster_series.resolve_page_count("做 1 张", settings=settings)
    assert error.value.code == "pages_per_group_limit"


def test_create_run_rejects_a_prompt_page_count_over_the_limit(tmp_path):
    settings = _settings(tmp_path, poster_series_max_pages=6)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    _asset(db, store, "asset-1", "封面图")
    payload = _payload(prompt="每组 9 张", reference_asset_ids=["asset-1"])
    with pytest.raises(poster_series.SeriesError) as error:
        _make_run(db, settings, store, payload, [])
    assert error.value.code == "pages_per_group_limit"
    # Nothing was persisted: the workspace mode stays unlocked for a corrected submission.
    assert db.whole_series_groups.count_documents({}) == 0
    assert db.whole_series_runs.count_documents({}) == 0


# ------------------------------------------------------------------ transient upload retry

class FlakyUploadCoreAI(FakeCoreAI):
    """Scripts upload outcomes per call so a partial failure is reproducible."""

    def __init__(self, plan=None) -> None:
        super().__init__()
        self.plan = list(plan or [])
        self.upload_attempts: list[int] = []

    def upload_image_blob(self, data: bytes, *, content_type: str = "image/png") -> str:
        self.upload_attempts.append(len(self.upload_attempts) + 1)
        outcome = self.plan.pop(0) if self.plan else None
        if isinstance(outcome, BaseException):
            raise outcome
        return super().upload_image_blob(data, content_type=content_type)


def _reset_error() -> ConnectionResetError:
    return ConnectionResetError(
        10054, "An existing connection was forcibly closed by the remote host",
    )


def test_asset_upload_retries_transient_errors_only(tmp_path):
    settings = _settings(tmp_path, poster_series_asset_upload_retry_seconds=0)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    _asset(db, store, "asset-1", "图1")
    db.whole_series_groups.insert_one({"_id": "g1", "agent_asset_urls": {}})
    group = db.whole_series_groups.find_one({"_id": "g1"})
    client = FlakyUploadCoreAI([_reset_error()])
    urls = poster_series._agent_asset_urls(db, settings, store, client, group, ["asset-1"])
    assert urls["asset-1"].startswith("https://core-ai.test/blob/")
    assert len(client.upload_attempts) == 2
    # The successful URL is stored immediately, so a later page never re-uploads it.
    assert db.whole_series_groups.find_one({"_id": "g1"})["agent_asset_urls"] == urls


def test_asset_upload_gives_up_after_the_bounded_attempts(tmp_path):
    settings = _settings(
        tmp_path, poster_series_asset_upload_retry_seconds=0,
        poster_series_asset_upload_attempts=3,
    )
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    _asset(db, store, "asset-1", "图1")
    db.whole_series_groups.insert_one({"_id": "g1", "agent_asset_urls": {}})
    group = db.whole_series_groups.find_one({"_id": "g1"})
    client = FlakyUploadCoreAI([TimeoutError("timed out")] * 3)
    with pytest.raises(poster_series.SeriesError) as error:
        poster_series._agent_asset_urls(db, settings, store, client, group, ["asset-1"])
    assert error.value.code == "asset_upload_failed"
    assert len(client.upload_attempts) == 3
    assert db.whole_series_groups.find_one({"_id": "g1"})["agent_asset_urls"] == {}


def test_asset_upload_does_not_retry_permanent_errors(tmp_path):
    settings = _settings(tmp_path, poster_series_asset_upload_retry_seconds=0)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    _asset(db, store, "asset-1", "图1")
    db.whole_series_groups.insert_one({"_id": "g1", "agent_asset_urls": {}})
    group = db.whole_series_groups.find_one({"_id": "g1"})
    from app.services.core_ai import CoreAIError

    client = FlakyUploadCoreAI([CoreAIError("empty_upload", "待上传图片为空", 422)])
    with pytest.raises(poster_series.SeriesError) as error:
        poster_series._agent_asset_urls(db, settings, store, client, group, ["asset-1"])
    assert error.value.code == "asset_upload_failed"
    assert len(client.upload_attempts) == 1
    assert "待上传图片为空" in error.value.message
    # A missing asset is rejected before any upload attempt and is never retried.
    missing = FlakyUploadCoreAI([])
    with pytest.raises(poster_series.SeriesError):
        poster_series._agent_asset_urls(db, settings, store, missing, group, ["asset-missing"])
    assert missing.upload_attempts == []


def test_asset_upload_partial_failure_only_reuploads_the_rest(tmp_path):
    settings = _settings(tmp_path, poster_series_asset_upload_retry_seconds=0)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    _asset(db, store, "asset-1", "图1")
    _asset(db, store, "asset-2", "图2")
    db.whole_series_groups.insert_one({"_id": "g1", "agent_asset_urls": {}})
    # asset-1 uploads on the first call; asset-2 then fails all three attempts.
    first = FlakyUploadCoreAI([None, _reset_error(), _reset_error(), _reset_error()])
    group = db.whole_series_groups.find_one({"_id": "g1"})
    with pytest.raises(poster_series.SeriesError):
        poster_series._agent_asset_urls(db, settings, store, first, group, ["asset-1", "asset-2"])
    stored = db.whole_series_groups.find_one({"_id": "g1"})["agent_asset_urls"]
    assert set(stored) == {"asset-1"}
    assert len(first.upload_attempts) == 4

    # The page retry keeps the stored URL and only uploads the asset that failed.
    second = FlakyUploadCoreAI([])
    group = db.whole_series_groups.find_one({"_id": "g1"})
    urls = poster_series._agent_asset_urls(db, settings, store, second, group, ["asset-1", "asset-2"])
    assert set(urls) == {"asset-1", "asset-2"}
    assert urls["asset-1"] == stored["asset-1"]
    assert len(second.upload_attempts) == 1


# ------------------------------------------------- series: the Logo is backend-owned only

def test_series_page_rejects_creating_a_replacement_that_requests_logo(tmp_path):
    from app.services import original_replacement

    settings, store, db, client, group, task, source = _generated_series_page(tmp_path)
    with pytest.raises(original_replacement.StudioError) as error:
        original_replacement.create(
            db, task["_id"], source_version_id=source["_id"], source_output_id="series_final",
            replacements=[{"target": "Logo", "asset_id": "logo-1"}],
        )
    assert error.value.code == "series_logo_replacement_not_allowed"
    assert error.value.status_code == 422
    assert "仅替换照片" in error.value.message
    # The official Logo asset under a photo-like target is still a Logo request.
    with pytest.raises(original_replacement.StudioError):
        original_replacement.create(
            db, task["_id"], source_version_id=source["_id"], source_output_id="series_final",
            replacements=[{"target": "角落图形", "asset_id": "logo-1"}],
        )
    assert db.background_replace_tasks.count_documents({}) == 0
    # A photo-only record on the same page is unaffected.
    created = original_replacement.create(
        db, task["_id"], source_version_id=source["_id"], source_output_id="series_final",
        replacements=[{"target": "内容图", "asset_id": "asset-1"}],
    )
    assert created["input"]["replacements"] == [{"target": "内容图", "asset_id": "asset-1"}]


def test_series_page_blocks_apply_of_a_logo_requesting_record(tmp_path):
    from app.services import original_replacement

    settings, store, db, client, group, task, source = _generated_series_page(tmp_path)
    _html_editor_context(db, task, source, replacements=[
        {"target": "Logo", "asset_id": "logo-1"},
    ], replacement_id="brep_logo")
    db.background_replace_tasks.update_one({"_id": "brep_logo"}, {"$set": {
        "status": "succeeded", "output": {"asset_id": "asset-1", "width": 1080, "height": 1350},
    }})
    with pytest.raises(original_replacement.StudioError) as error:
        original_replacement.apply(
            db, task["_id"], "brep_logo",
            expected_current_version_id=source["_id"],
            expected_current_output_id="series_final",
            settings=settings, store=store,
        )
    assert error.value.code == "series_logo_replacement_not_allowed"
    assert "重新生成仅替换照片" in error.value.message
    current = db.whole_tasks.find_one({"_id": task["_id"]})
    assert current["current_version_id"] == source["_id"]


def test_html_save_rejects_an_editor_result_from_a_logo_requesting_record(tmp_path):
    settings, store, db, client, group, task, source = _generated_series_page(tmp_path)
    page_id = group["pages"][0]["page_id"]
    _html_editor_context(db, task, source, replacements=[
        {"target": "Logo", "asset_id": "logo-1"},
    ], artifact_id="wart_logo", replacement_id="brep_logo_html")
    payload = _html_payload(task, group, artifact_id="wart_logo", session="session-logo-01")
    with pytest.raises(poster_series.SeriesError) as error:
        poster_series.html_save(db, settings, store, group["id"], page_id, payload, merchant_id=MERCHANT)
    assert error.value.code == "series_logo_replacement_not_allowed"
    assert error.value.status_code == 422
    assert "重新生成仅替换照片" in error.value.message
    assert db.whole_series_html_saves.count_documents({}) == 0
    assert db.whole_versions.count_documents({"task_id": task["_id"]}) == 1
