from __future__ import annotations

import io
import threading
import time
from pathlib import Path

import mongomock
from PIL import Image

from app.config import Settings
from app.routes import poster_edit as routes
from pydantic import ValidationError

from app.schemas import PosterEditBatchRequest, PosterEditRequest
from app.services import agent_image, poster_edit, storage
from app.services.core_ai import AgentRunResult, CoreAIError, DownloadedAgentImage
from app.services.object_store import LocalObjectStore


class FakeAgent:
    def __init__(self) -> None:
        self.urls = 0
        self.calls: list[dict] = []
        self.fail_calls: set[int] = set()
        self.delay = 0.0
        self.active = 0
        self.max_active = 0
        self.resumed_run_ids: list[str] = []
        self.on_run_id_hook = None
        self._lock = threading.Lock()

    def upload_image_blob(self, data: bytes, *, content_type: str = "image/png") -> str:
        self.urls += 1
        return f"https://example.com/ref-{self.urls}.png"

    def run_agent_detail(self, agent_id: str, input_text: str, *, timeout_seconds: int, poll_interval: float = 2.0, on_run_id=None):
        with self._lock:
            call_number = len(self.calls) + 1
            self.calls.append({"agent_id": agent_id, "input": input_text, "timeout_seconds": timeout_seconds})
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            run_id = f"run-poster-{call_number}"
            if on_run_id:
                on_run_id(run_id)
            if self.on_run_id_hook:
                self.on_run_id_hook(run_id)
            if self.delay:
                time.sleep(self.delay)
            if call_number in self.fail_calls:
                raise CoreAIError("agent_failed", f"candidate {call_number} failed")
            return AgentRunResult(run_id=run_id, status="COMPLETED", output="done")
        finally:
            with self._lock:
                self.active -= 1

    def wait_agent_detail(self, run_id: str, *, timeout_seconds: int, poll_interval: float = 2.0):
        self.resumed_run_ids.append(run_id)
        return AgentRunResult(run_id=run_id, status="COMPLETED", output="resumed")

    def download_agent_images(self, result: AgentRunResult):
        transparent = Image.new("RGBA", (320, 320), (0, 0, 0, 0))
        for x in range(60, 260):
            for y in range(60, 140):
                transparent.putpixel((x, y), (20, 80, 180, 255))
        layer = io.BytesIO()
        transparent.save(layer, format="PNG")
        final = io.BytesIO()
        Image.new("RGB", (320, 320), (230, 210, 180)).save(final, format="PNG")
        return [
            DownloadedAgentImage(layer.getvalue(), "https://example.com/layer.png", "layer.png", "image/png"),
            DownloadedAgentImage(final.getvalue(), "https://example.com/final.png", "final.png", "image/png"),
        ]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        storage_dir=str(tmp_path / "storage"),
        sample_dir=str(tmp_path / "samples"),
        core_ai_api_key="test-key",
        core_ai_image_agent_timeout_seconds=900,
    )


def _seed(db, settings, store, name: str, color) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (320, 240), color).save(buffer, format="PNG")
    prepared = storage.prepare_upload(settings, store, buffer.getvalue(), name)
    db.assets.insert_one({
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
    })
    return prepared.id


def test_agent_edit_input_only_contains_user_request_and_ordered_urls():
    input_text = agent_image.build_agent_edit_input(
        [
            ("参考图 1", "https://example.com/poster.png"),
            ("参考图 2", "https://example.com/background.png"),
        ],
        "帮我改成圣诞节主题，10月31日当天改成12月25日当天",
    )

    assert input_text == (
        "帮我改成圣诞节主题，10月31日当天改成12月25日当天\n\n"
        "https://example.com/poster.png\n"
        "https://example.com/background.png"
    )


def test_direct_poster_edit_runs_agent_and_saves_both_outputs(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    first = _seed(db, settings, store, "poster.png", (30, 60, 90))
    second = _seed(db, settings, store, "background.png", (200, 120, 60))
    fake = FakeAgent()

    task = poster_edit.create_task(
        db,
        prompt="把图一的背景换成图二，保留所有文字",
        asset_ids=[first, second],
    )
    poster_edit.run_task(db, settings, store, fake, task["_id"])

    stored = db.poster_edit_tasks.find_one({"_id": task["_id"]})
    assert stored["status"] == "succeeded"
    assert [item["role"] for item in stored["outputs"]] == ["agent_transparent", "agent_result"]
    assert fake.calls[0]["agent_id"] == settings.core_ai_image_agent_id
    assert fake.calls[0]["timeout_seconds"] == 900
    assert "https://example.com/ref-1.png" in fake.calls[0]["input"]
    assert "https://example.com/ref-2.png" in fake.calls[0]["input"]
    assert fake.calls[0]["input"] == (
        "把图一的背景换成图二，保留所有文字\n\n"
        "https://example.com/ref-1.png\n"
        "https://example.com/ref-2.png"
    )

    out = routes.get_poster_edit(task["_id"], type("Request", (), {"app": type("App", (), {"state": type("State", (), {"db": db})()})()})())
    assert len(out["outputs"]) == 2
    assert out["outputs"][0]["role"] == "agent_transparent"
    assert out["outputs"][1]["selectable"] is True
    assert out["outputs"][0]["width"] == 320
    assert out["outputs"][0]["height"] == 240
    assert out["outputs"][1]["width"] == 320
    assert out["outputs"][1]["height"] == 240
    assert out["references"][0]["preview_url"].startswith("/api/assets/")
    assert out["created_at"].endswith("Z")
    assert out["updated_at"].endswith("Z")


def test_direct_poster_edit_requires_one_to_five_images(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    try:
        poster_edit.create_task(db, prompt="修改", asset_ids=[])
    except poster_edit.PosterEditError as exc:
        assert exc.code == "reference_required"
    else:
        raise AssertionError("expected validation error")


def test_batch_creates_requested_candidate_count_and_uploads_refs_once(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    first = _seed(db, settings, store, "poster.png", (30, 60, 90))
    second = _seed(db, settings, store, "background.png", (200, 120, 60))

    for count in (1, 3, 6):
        fake = FakeAgent()
        batch_id, created = poster_edit.create_batch(
            db,
            prompt="批量修改",
            asset_ids=[first, second],
            candidate_count=count,
        )
        assert len(created) == count
        assert all(task["batch_id"] == batch_id for task in created)
        poster_edit.run_batch(db, settings, store, fake, batch_id)

        stored = list(
            db.poster_edit_tasks.find({"batch_id": batch_id}).sort("candidate_index", 1)
        )
        assert [task["candidate_index"] for task in stored] == list(range(count))
        assert all(task["status"] == "succeeded" for task in stored)
        assert fake.urls == 2
        assert len(fake.calls) == count
        assert len({call["input"] for call in fake.calls}) == 1
        assert all(len(task["outputs"]) == 2 for task in stored)


def test_batch_rejects_invalid_candidate_count(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    asset_id = _seed(db, settings, store, "poster.png", (30, 60, 90))

    for count in (0, 7):
        try:
            poster_edit.create_batch(
                db,
                prompt="批量修改",
                asset_ids=[asset_id],
                candidate_count=count,
            )
        except poster_edit.PosterEditError as exc:
            assert exc.code == "candidate_count_invalid"
        else:
            raise AssertionError("expected invalid candidate count")


def test_batch_runs_in_parallel_and_keeps_successful_candidates(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    asset_id = _seed(db, settings, store, "poster.png", (30, 60, 90))
    fake = FakeAgent()
    fake.delay = 0.08
    fake.fail_calls = {2}
    batch_id, _ = poster_edit.create_batch(
        db,
        prompt="批量修改",
        asset_ids=[asset_id],
        candidate_count=3,
    )

    poster_edit.run_batch(db, settings, store, fake, batch_id)

    stored = list(
        db.poster_edit_tasks.find({"batch_id": batch_id}).sort("candidate_index", 1)
    )
    statuses = [task["status"] for task in stored]
    assert statuses.count("succeeded") == 2
    assert statuses.count("failed") == 1
    assert fake.max_active >= 2
    assert [len(task["outputs"]) for task in stored] == [
        2 if status == "succeeded" else 0 for status in statuses
    ]
    batch = poster_edit.batch_out(db, batch_id)
    assert [task["candidate_index"] for task in batch["tasks"]] == [0, 1, 2]


def test_poster_edit_persists_run_id_as_soon_as_agent_is_triggered(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    asset_id = _seed(db, settings, store, "poster.png", (30, 60, 90))
    fake = FakeAgent()
    observed: list[str] = []
    task = poster_edit.create_task(db, prompt="修改海报", asset_ids=[asset_id])

    def assert_persisted(run_id: str) -> None:
        stored = db.poster_edit_tasks.find_one({"_id": task["_id"]})
        observed.append(str(stored.get("run_id") or ""))

    fake.on_run_id_hook = assert_persisted
    poster_edit.run_task(db, settings, store, fake, task["_id"])

    assert observed == ["run-poster-1"]
    assert db.poster_edit_tasks.find_one({"_id": task["_id"]})["run_id"] == "run-poster-1"


def test_poster_edit_resumes_existing_agent_run_without_retriggering(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    asset_id = _seed(db, settings, store, "poster.png", (30, 60, 90))
    fake = FakeAgent()
    task = poster_edit.create_task(db, prompt="修改海报", asset_ids=[asset_id])
    db.poster_edit_tasks.update_one(
        {"_id": task["_id"]},
        {
            "$set": {
                "status": "running",
                "stage": "agent_running",
                "progress": 30,
                "run_id": "run-existing",
                "agent_input": "existing input",
                "agent_id": settings.core_ai_image_agent_id,
            }
        },
    )

    poster_edit.resume_tasks(db, settings, store, fake)

    stored = db.poster_edit_tasks.find_one({"_id": task["_id"]})
    assert stored["status"] == "succeeded"
    assert stored["run_id"] == "run-existing"
    assert fake.calls == []
    assert fake.resumed_run_ids == ["run-existing"]


def test_mark_interrupted_keeps_runs_with_run_id_resumable(tmp_path):
    settings = _settings(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    db = mongomock.MongoClient().db
    asset_id = _seed(db, settings, store, "poster.png", (30, 60, 90))
    resumable = poster_edit.create_task(db, prompt="可续传", asset_ids=[asset_id])
    interrupted = poster_edit.create_task(db, prompt="不可续传", asset_ids=[asset_id])
    db.poster_edit_tasks.update_one(
        {"_id": resumable["_id"]},
        {"$set": {"status": "running", "stage": "agent_running", "run_id": "run-keep"}},
    )
    db.poster_edit_tasks.update_one(
        {"_id": interrupted["_id"]},
        {"$set": {"status": "running", "stage": "preparing"}},
    )

    count = poster_edit.mark_interrupted(db)

    assert count == 1
    assert db.poster_edit_tasks.find_one({"_id": resumable["_id"]})["status"] == "running"
    assert db.poster_edit_tasks.find_one({"_id": interrupted["_id"]})["status"] == "interrupted"


def test_poster_edit_batch_request_rejects_invalid_candidate_count():
    for value in (0, 7, "invalid"):
        try:
            PosterEditBatchRequest(prompt="修改", asset_ids=["asset_1"], candidate_count=value)
        except ValidationError:
            pass
        else:
            raise AssertionError(f"expected validation error for {value!r}")
