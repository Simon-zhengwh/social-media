from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from types import SimpleNamespace

import mongomock
import pytest
from fastapi import HTTPException
from PIL import Image

from app.main import app
from app.routes import task_center
from app.routes.assets import delete_asset
from app.services import storage
from app.services.core_ai import AgentArtifact, AgentRunResult, CoreAIError
from tests.conftest import png_bytes


def test_background_replacement_routes_exist():
    paths = app.openapi()["paths"]
    assert "/api/merchants/{merchant_id}/background-replacements" in paths
    assert "/api/merchants/{merchant_id}/background-replacements/{task_id}/refresh-result" in paths


class FakeAgent:
    def __init__(self):
        self.triggers = []
        self.polls = []
        self.pending = False
        self.output = "已完成\n![蒙版](https://example.com/mask.png)\n![替换结果](https://example.com/final.png)"
        self.data = png_bytes()
        self.downloads = []
        self.download_error = False
        self.on_trigger = None
        self.uploads = []
        self.artifacts = []

    def upload_image_blob(self, data, *, content_type):
        self.uploads.append(data)
        return f"https://example.com/input-{len(self.uploads)}.png"

    def trigger_agent(self, agent_id, input_text, *, timeout_seconds):
        self.triggers.append((agent_id, input_text, timeout_seconds))
        if self.on_trigger:
            self.on_trigger()
        return "run-replacement"

    def poll_agent_detail(self, run_id):
        self.polls.append(run_id)
        if self.pending:
            return None
        return AgentRunResult(run_id=run_id, status="COMPLETED", output=self.output, artifacts=self.artifacts)

    def download_image(self, url):
        self.downloads.append(url)
        if self.download_error:
            raise CoreAIError("download_failed", "下载失败")
        return self.data


@pytest.fixture
def env(settings, store):
    from app.services import background_replace as service

    db = mongomock.MongoClient().db
    for asset_id in ["source", "beef", "person"]:
        prepared = storage.prepare_upload(settings, store, png_bytes(), asset_id + ".png")
        db.assets.insert_one({"_id": asset_id, "original_name": asset_id + ".png", "path": prepared.path,
                              "prepared_path": prepared.prepared_path, "width": 320, "height": 240,
                              "mime_type": "image/png"})
    return SimpleNamespace(service=service, db=db, settings=settings, store=store, client=FakeAgent())


def create(env, replacements=None):
    return env.service.create_task(env.db, source_asset_id="source", replacements=replacements or [
        {"target": "右下角牛肉", "asset_id": "beef"}, {"target": "左上角人物", "asset_id": "person"}])


def run(env, task, **kwargs):
    env.service.run_task(env.db, env.settings, env.store, env.client, task["_id"], **kwargs)
    return env.db.background_replace_tasks.find_one({"_id": task["_id"]})


def test_ordered_mapping_and_reused_material_are_preserved(env):
    task = create(env, [{"target": "牛肉", "asset_id": "beef"}, {"target": "左上人物", "asset_id": "beef"}])
    result = run(env, task)
    agent, text, timeout = env.client.triggers[0]
    assert agent == "d1692e66-c34b-4e70-9437-7462258dd84e"
    assert timeout == 2700
    assert text == (
        "帮我把图1的牛肉替换成图2，并且把图1的左上人物替换成图2\n\n"
        "https://example.com/input-1.png\nhttps://example.com/input-2.png"
    )
    assert len(result["input"]["replacements"]) == 2
    assert result["status"] == "succeeded"
    assert result["deadline_at"] - result["started_at"] == timedelta(seconds=2700)
    assert env.client.downloads == ["https://example.com/final.png"]
    saved = env.db.assets.find_one({"_id": result["output"]["asset_id"]})
    with Image.open(env.store.local_path(saved["path"])) as image:
        assert image.size == (320, 240)


@pytest.mark.parametrize("items", [[], [{"target": " ", "asset_id": "beef"}],
    [{"target": "牛肉", "asset_id": "missing"}], [{"target": "牛肉", "asset_id": "beef"}] * 6])
def test_invalid_replacements_are_rejected(env, items):
    with pytest.raises(env.service.BackgroundReplaceError):
        env.service.create_task(env.db, source_asset_id="source", replacements=items)
    assert env.db.background_replace_tasks.count_documents({}) == 0


def test_missing_source_and_non_image_are_rejected(env):
    with pytest.raises(env.service.BackgroundReplaceError):
        env.service.create_task(env.db, source_asset_id="missing", replacements=[{"target": "牛肉", "asset_id": "beef"}])
    env.db.assets.update_one({"_id": "beef"}, {"$set": {"mime_type": "application/pdf"}})
    with pytest.raises(env.service.BackgroundReplaceError):
        create(env)


@pytest.mark.parametrize("output,code", [
    ("请问要替换左边还是右边？", "agent_image_missing"),
    ("![蒙版](https://example.com/mask.png)", "agent_image_missing"),
    ("![替换结果](https://example.com/a.png)\n![替换结果](https://example.com/b.png)", "agent_image_ambiguous"),
])
def test_missing_or_ambiguous_final_image_is_not_success(env, output, code):
    env.client.output = output
    result = run(env, create(env))
    assert result["status"] == "partial"
    assert result["error"]["code"] == code
    assert result["agent_output"] == output
    assert not env.client.downloads


def test_background_request_is_only_natural_instruction_and_ordered_urls(env):
    run(env, create(env, [{"target": "背景", "asset_id": "beef"}]))
    assert env.client.triggers[0][1] == (
        "帮我把图1的背景替换成图2\n\n"
        "https://example.com/input-1.png\nhttps://example.com/input-2.png"
    )


@pytest.mark.parametrize("reply", [
    "已完成。![最终成图](https://example.com/final.png)",
    "已完成。[下载最终 PNG](https://example.com/final.png)",
    "![成图](https://example.com/final.png)\n![对比预览](https://example.com/comparison.png)",
    "![图片](https://example.com/final.png)\n[蒙版](https://example.com/mask.png)",
])
def test_natural_agent_replies_do_not_require_fixed_image_caption(env, reply):
    env.client.output = reply
    result = run(env, create(env))
    assert result["status"] == "succeeded"
    assert env.client.downloads == ["https://example.com/final.png"]


def test_final_artifact_is_used_when_reply_has_no_image_link(env):
    env.client.output = "已完成替换，最终 PNG 见附件。"
    env.client.artifacts = [
        AgentArtifact("mask.png", "image/png", "https://example.com/mask.png"),
        AgentArtifact("poster-final.png", "image/png", "https://example.com/final.png"),
        AgentArtifact("before-after.png", "image/png", "https://example.com/comparison.png"),
    ]
    result = run(env, create(env))
    assert result["status"] == "succeeded"
    assert env.client.downloads == ["https://example.com/final.png"]


def test_only_auxiliary_artifacts_do_not_count_as_final_image(env):
    env.client.output = "暂未完成，只提供蒙版。"
    env.client.artifacts = [AgentArtifact("mask-final.png", "image/png", "https://example.com/mask.png")]
    assert run(env, create(env))["status"] == "partial"
    assert not env.client.downloads


def test_wrong_size_is_rejected_without_resizing(env):
    env.client.data = png_bytes(200, 200)
    result = run(env, create(env))
    assert result["status"] == "failed"
    assert result["error"]["code"] == "output_size_mismatch"
    assert result["output"] is None


def test_original_resolution_is_used_instead_of_prepared_thumbnail(env):
    original = png_bytes(3000, 2000)
    prepared = storage.prepare_upload(env.settings, env.store, original, "large-original.png")
    env.db.assets.update_one({"_id": "source"}, {"$set": {
        "path": prepared.path, "prepared_path": prepared.prepared_path,
        "width": prepared.width, "height": prepared.height}})
    assert prepared.width < 3000
    env.client.data = original
    result = run(env, create(env))
    assert result["status"] == "succeeded"
    assert result["source"]["width"] == result["output"]["width"] == 3000
    assert original in env.client.uploads


def test_output_orientation_is_checked_before_success(env):
    import io

    image = Image.new("RGB", (320, 240), "red")
    exif = image.getexif()
    exif[274] = 6
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", exif=exif)
    env.client.data = buffer.getvalue()
    result = run(env, create(env))
    assert result["status"] == "failed"
    assert result["error"]["code"] == "output_size_mismatch"


def test_saving_failure_can_be_recovered_without_triggering_again(env):
    task = create(env)
    env.client.download_error = True
    assert run(env, task)["status"] == "failed"
    env.client.download_error = False
    env.service.queue_refresh(env.db, task["_id"])
    assert run(env, task, refresh=True)["status"] == "succeeded"
    assert len(env.client.triggers) == 1


def test_refresh_pending_run_continues_until_result_without_resetting_deadline(env, monkeypatch):
    now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(env.service, "utcnow", lambda: now)
    monkeypatch.setattr(env.service.time, "sleep", lambda _: None)
    task = create(env)
    deadline = now + timedelta(minutes=25)
    env.db.background_replace_tasks.update_one({"_id": task["_id"]}, {"$set": {
        "status": "failed", "run_id": "run-existing", "deadline_at": deadline,
        "error": {"code": "agent_timeout", "message": "old error"}}})
    env.service.queue_refresh(env.db, task["_id"])
    original_poll = env.client.poll_agent_detail

    def poll(run_id):
        saved = env.db.background_replace_tasks.find_one({"_id": task["_id"]})
        assert saved["status"] == "running"
        assert saved["stage"] == "agent_running"
        assert saved["error"] is None
        if not env.client.polls:
            env.client.polls.append(run_id)
            return None
        return original_poll(run_id)

    env.client.poll_agent_detail = poll
    result = run(env, task, refresh=True)
    assert result["status"] == "succeeded"
    assert result["error"] is None
    assert result["deadline_at"] == deadline.replace(tzinfo=None)
    assert env.client.polls == ["run-existing", "run-existing"]
    assert not env.client.triggers


def test_refresh_pending_run_stops_at_original_deadline(env, monkeypatch):
    now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(env.service, "utcnow", lambda: now)

    def sleep(seconds):
        nonlocal now
        now += timedelta(seconds=seconds)

    monkeypatch.setattr(env.service.time, "sleep", sleep)
    task = create(env)
    deadline = now + timedelta(seconds=5)
    env.db.background_replace_tasks.update_one({"_id": task["_id"]}, {"$set": {
        "status": "failed", "run_id": "run-existing", "deadline_at": deadline}})
    env.client.pending = True
    env.service.queue_refresh(env.db, task["_id"])
    result = run(env, task, refresh=True)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "agent_timeout"
    assert now == deadline
    assert result["deadline_at"] == deadline.replace(tzinfo=None)
    assert env.client.polls == ["run-existing", "run-existing"]
    assert not env.client.triggers


def test_timeout_uses_persisted_deadline_then_manual_query_recovers(env, monkeypatch):
    now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(env.service, "utcnow", lambda: now)
    monkeypatch.setattr(env.service.time, "sleep", lambda seconds: None)
    task = create(env)
    env.client.pending = True
    original_poll = env.client.poll_agent_detail

    def advance(run_id):
        nonlocal now
        now += timedelta(seconds=2700)
        return original_poll(run_id)

    env.client.poll_agent_detail = advance
    result = run(env, task)
    assert result["error"]["code"] == "agent_timeout"
    assert result["run_id"] == "run-replacement"
    deadline = result["deadline_at"]
    env.client.pending = False
    env.service.queue_refresh(env.db, task["_id"])
    result = run(env, task, refresh=True)
    assert result["status"] == "succeeded"
    assert result["deadline_at"] == deadline
    assert len(env.client.triggers) == 1


def test_run_id_is_persisted_before_polling(env):
    task = create(env)
    original = env.client.poll_agent_detail

    def poll(run_id):
        saved = env.db.background_replace_tasks.find_one({"_id": task["_id"]})
        assert saved["run_id"] == run_id
        assert saved["deadline_at"] and saved["agent_input"]
        return original(run_id)

    env.client.poll_agent_detail = poll
    run(env, task)


@pytest.mark.parametrize("expired", [False, True])
def test_restart_resumes_same_run_without_extending_deadline(env, expired):
    task = create(env)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=-1 if expired else 120)
    env.db.background_replace_tasks.update_one({"_id": task["_id"]}, {"$set": {
        "status": "running", "run_id": "run-old", "agent_input": "saved input", "deadline_at": deadline}})
    stored_deadline = env.db.background_replace_tasks.find_one({"_id": task["_id"]})["deadline_at"]
    env.service.mark_interrupted(env.db)
    env.service.resume_tasks(env.db, env.settings, env.store, env.client)
    result = env.db.background_replace_tasks.find_one({"_id": task["_id"]})
    assert result["status"] == "succeeded"
    assert not env.client.triggers
    assert env.client.polls == ["run-old"]
    assert result["deadline_at"] == stored_deadline


def test_untriggered_task_is_interrupted_on_restart(env):
    task = create(env)
    env.service.mark_interrupted(env.db)
    result = env.db.background_replace_tasks.find_one({"_id": task["_id"]})
    assert result["status"] == "interrupted"
    with pytest.raises(env.service.BackgroundReplaceError):
        env.service.queue_refresh(env.db, task["_id"])


def test_refresh_cannot_trigger_a_queued_task_without_run_id(env):
    task = create(env)
    with pytest.raises(env.service.BackgroundReplaceError):
        env.service.queue_refresh(env.db, task["_id"])
    assert not env.client.triggers


def test_duplicate_worker_does_not_trigger_again(env):
    task = create(env)
    run(env, task)
    run(env, task)
    assert len(env.client.triggers) == 1


def test_route_queues_work_without_waiting_and_restores_inputs(env):
    from fastapi import BackgroundTasks
    from app.routes import background_replace as routes
    from app.schemas import BackgroundReplacementRequest

    background = BackgroundTasks()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        db=env.db, settings=env.settings, store=env.store, core_ai=env.client)))
    payload = BackgroundReplacementRequest(source_asset_id="source", replacements=[{"target": "牛肉", "asset_id": "beef"}])
    response = routes.create_replacement(payload, request, background)
    assert response["status"] == "queued"
    assert len(background.tasks) == 1
    assert env.client.triggers == []
    restored = routes.get_replacement(response["id"], request)
    assert restored["source"]["asset_id"] == "source"
    assert restored["replacements"][0]["target"] == "牛肉"
    assert app.openapi()["paths"]["/api/merchants/{merchant_id}/background-replacements"]["post"]["responses"].get("202")
    with pytest.raises(HTTPException) as exc:
        routes.download_replacement(response["id"], request)
    assert exc.value.status_code == 404


def test_temporary_network_error_retries_without_new_trigger(env, monkeypatch):
    import httpx

    original_poll = env.client.poll_agent_detail
    attempts = 0

    def poll(run_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("temporary failure")
        return original_poll(run_id)

    monkeypatch.setattr(env.service.time, "sleep", lambda _: None)
    env.client.poll_agent_detail = poll
    result = run(env, create(env))
    assert result["status"] == "succeeded"
    assert attempts == 2 and len(env.client.triggers) == 1


def test_background_task_is_listed_separately_and_assets_are_protected(env):
    result = run(env, create(env))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=env.db, store=env.store)))
    page = task_center.list_task_center(request, kind="background_replace")
    assert page["total"] == 1
    item = page["items"][0]
    assert item["kind"] == "background_replace"
    assert item["id"] == result["_id"]
    assert item["status"] == "ready"
    assert item["download_url"].startswith("/api/background-replacements/")
    assert task_center.list_task_center(request, kind="edit")["total"] == 0
    for asset_id in ["source", "beef", "person"]:
        with pytest.raises(HTTPException) as exc:
            delete_asset(asset_id, request)
        assert exc.value.status_code == 409


@pytest.mark.skipif(os.name != "nt", reason="Windows extended path normalization")
def test_output_storage_accepts_equivalent_windows_extended_path(settings, store, monkeypatch):
    original = Path.resolve

    def extended(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path.name == "output.png":
            return Path("\\\\?\\" + str(result))
        return result

    monkeypatch.setattr(Path, "resolve", extended)
    store.put_bytes("outputs/output.png", png_bytes(), "image/png")
    assert store.get_bytes("outputs/output.png") == png_bytes()
    from app.services.object_store import ObjectStoreError
    with pytest.raises(ObjectStoreError):
        store.put_bytes("../output.png", png_bytes(), "image/png")
