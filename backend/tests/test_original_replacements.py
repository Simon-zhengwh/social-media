import base64
from datetime import timedelta
from io import BytesIO
from types import SimpleNamespace

import mongomock
import pytest
from fastapi import BackgroundTasks, HTTPException
from PIL import Image
from pydantic import ValidationError

from app import schemas
from app.main import app
from app.routes import task_center, whole_image as routes
from app.services import original_replacement_runner, storage, whole_image
from app.services.core_ai import AgentArtifact
from tests.conftest import png_bytes
from tests.test_original_replacement_runner import FakeAgent
from tests.test_whole_image import FakeCoreAI, _request


@pytest.fixture
def env(settings, store, request):
    db = mongomock.MongoClient().db
    generator = FakeCoreAI()
    created = routes.create_task(schemas.WholeImageTaskCreate(prompt="夏日冰美式海报", ratio=getattr(request, "param", "1:1")),
                                 _request(db, settings, store, generator), BackgroundTasks())
    task_id = created["id"]
    whole_image.run_generation(db, settings, store, generator, task_id)
    task = db.whole_tasks.find_one({"_id": task_id})
    for asset_id in ("beef", "environment", "logo"):
        image = storage.prepare_upload(settings, store, png_bytes(), asset_id + ".png")
        db.assets.insert_one({"_id": asset_id, "original_name": asset_id + ".png",
                              "path": image.path, "prepared_path": image.prepared_path,
                              "mime_type": "image/png", "width": 320, "height": 240})
    client = FakeAgent()
    client.data = png_bytes(task["canvas"]["width"], task["canvas"]["height"])
    return SimpleNamespace(db=db, settings=settings, store=store, client=client, task_id=task_id,
                           source_id=task["current_version_id"], output_id=task["current_output_id"],
                           request=_request(db, settings, store, client))


def create(env, items=None, **overrides):
    values = {"source_version_id": env.source_id, "source_output_id": env.output_id,
              "replacements": items if items is not None else [{"target": "右下角牛肉", "asset_id": "beef"}]}
    values.update(overrides)
    return routes.create_original_replacement(env.task_id,
        schemas.WholeImageOriginalReplacementRequest(**values), env.request, BackgroundTasks())


def complete(env, task):
    original_replacement_runner.run_task(env.db, env.settings, env.store, env.client, task["id"])
    return original_replacement_runner.get_task(env.db, task["id"])


def apply(env, task, **overrides):
    current = env.db.whole_tasks.find_one({"_id": env.task_id})
    values = {"expected_current_version_id": current["current_version_id"],
              "expected_current_output_id": current["current_output_id"]}
    values.update(overrides)
    return routes.apply_original_replacement(env.task_id, task["id"],
        schemas.WholeImageOriginalReplacementApplyRequest(**values), env.request)


def run_background(background):
    (job,) = background.tasks
    job.func(*job.args, **job.kwargs)


class LogoAwareCoreAI(FakeCoreAI):
    """FakeCoreAI that counts Logo location calls and can answer them deterministically."""

    def __init__(self, logo_payload=None, logo_error: Exception | None = None) -> None:
        super().__init__()
        self.chat_calls = 0
        self.chat_model: str | None = None
        self.logo_payload = {"status": "pass", "violations": []} if logo_payload is None else logo_payload
        self.logo_error = logo_error

    def chat_json(self, messages, *, model=None, timeout_seconds=None):
        self.chat_calls += 1
        self.chat_model = model
        if self.logo_error is not None:
            raise self.logo_error
        return self.logo_payload


def model_batch(env, items=None, candidate_model=None, client=None):
    env.client = client or LogoAwareCoreAI()
    env.request.app.state.core_ai = env.client
    background = BackgroundTasks()
    task = routes.create_original_replacement(env.task_id,
        schemas.WholeImageOriginalReplacementRequest(
            source_version_id=env.source_id, source_output_id=env.output_id, kind="model",
            candidate_model=candidate_model,
            replacements=items or [{"target": "右下角牛肉", "asset_id": "beef"}]),
        env.request, background)
    assert task["kind"] == "model"
    return task, background


def test_model_replacement_sends_one_request_and_leaves_the_other_models_idle(env):
    task, background = model_batch(env)
    run_background(background)
    restored = routes.list_original_replacements(env.task_id, env.request)[0]
    assert restored["id"] == task["id"]
    assert restored["status"] == "succeeded"
    # One click spends one model request instead of three.
    assert [call["model"] for call in env.client.edit_calls] == ["seedream-5-pro-image-to-image"]
    assert [(item["model"], item["status"]) for item in restored["candidates"]] == [
        ("seedream-5-pro-image-to-image", "succeeded"),
        ("seedream-5.5-image-to-image", "idle"),
        ("minimax-h3-image-edit", "idle")]
    for item in restored["candidates"][:3]:
        assert item["output"] is None if item["status"] == "idle" else item["output"] is not None
    assert env.db.whole_tasks.find_one({"_id": env.task_id})["current_version_id"] == env.source_id


def test_idle_candidate_runs_on_demand_with_the_shared_input(env):
    task, background = model_batch(env, [{"target": "右下角牛肉", "asset_id": "beef"},
        {"target": "", "asset_id": "environment"}, {"target": "Logo", "asset_id": "logo"}])
    run_background(background)
    baseline = env.db.background_replace_tasks.find_one({"_id": task["id"]})
    env.client.edit_calls.clear()
    retry = BackgroundTasks()
    response = routes.retry_original_replacement_candidate(env.task_id, task["id"], "seedream-5.5-image-to-image",
                                                          env.request, retry)
    assert [item["status"] for item in response["candidates"]] == ["succeeded", "queued", "idle"]
    run_background(retry)
    assert [call["model"] for call in env.client.edit_calls] == ["seedream-5.5-image-to-image"]
    assert env.client.edit_calls[0]["prompt"] == baseline["candidates"][0]["model_input"]["prompt"]
    # The official Logo file never reaches the edit model: the workbench composites it later.
    assert env.client.edit_calls[0]["reference_names"][1:] == ["beef.png", "environment.png"]
    assert "不要在画面任何位置绘制任何品牌 Logo" in baseline["candidates"][0]["model_input"]["prompt"]
    assert [item["asset_id"] for item in baseline["candidates"][0]["model_input"]["images"]] == [
        task["source"]["asset_id"], "beef", "environment"]
    assert baseline["logo_overlay"]["asset_id"] == "logo"
    restored = env.db.background_replace_tasks.find_one({"_id": task["id"]})
    assert restored["status"] == "succeeded"
    assert [item["status"] for item in restored["candidates"]] == ["succeeded", "succeeded", "idle"]
    assert restored["candidates"][1]["output"]["width"] == task["source"]["width"]
    # One location per batch: the second candidate reused it instead of calling the model again.
    assert restored["logo_overlay"] == baseline["logo_overlay"]
    assert env.client.chat_calls == 1


@pytest.mark.parametrize("env", ["4:5"], indirect=True)
def test_requested_model_that_cannot_keep_the_source_ratio_is_rejected_upfront(env):
    with pytest.raises(HTTPException) as unsupported:
        model_batch(env, candidate_model="seedream-5.5-image-to-image")
    assert unsupported.value.status_code == 422
    assert unsupported.value.detail == "所选模型不支持当前底稿的比例，请换一个模型"
    assert env.db.background_replace_tasks.count_documents({}) == 0


@pytest.mark.parametrize("env", ["4:5"], indirect=True)
def test_model_replacement_skips_candidates_that_cannot_keep_the_source_ratio(env):
    task, background = model_batch(env)
    edit = env.client.edit_image

    def edit_image(prompt, refs, *, size, model):
        edit(prompt, refs, size=size, model=model)
        return png_bytes(1088, 1360)

    env.client.edit_image = edit_image
    assert (task["source"]["width"], task["source"]["height"]) == (1080, 1350)
    run_background(background)
    assert [call["model"] for call in env.client.edit_calls] == ["minimax-h3-image-edit"]
    assert [(candidate["status"], candidate["error"]) for candidate in task["candidates"]] == [
        ("unsupported", "不支持此格式"), ("unsupported", "不支持此格式"), ("queued", None),
    ]
    restored = routes.list_original_replacements(env.task_id, env.request)[0]
    assert restored["status"] == "succeeded"
    assert [candidate["status"] for candidate in restored["candidates"]] == ["unsupported", "unsupported", "succeeded"]
    assert restored["candidates"][2]["output"]["height"] == 1350


@pytest.mark.parametrize("env", ["4:5"], indirect=True)
def test_historical_candidate_with_an_unsupported_ratio_cannot_be_retried(env):
    task, _ = model_batch(env)
    env.db.background_replace_tasks.update_one({"_id": task["id"]},
        {"$set": {"status": "failed", "candidates.0.status": "failed", "candidates.0.error": "模型返回的画布比例与底稿不符"}})
    retry = BackgroundTasks()
    with pytest.raises(HTTPException) as unsupported:
        routes.retry_original_replacement_candidate(env.task_id, task["id"], "seedream-5-pro-image-to-image",
                                                    env.request, retry)
    assert unsupported.value.status_code == 422
    assert unsupported.value.detail == "不支持此格式"
    assert not retry.tasks


@pytest.mark.parametrize("env", ["4:5"], indirect=True)
def test_explicit_crop_request_keeps_full_model_output_without_applying_it(env):
    task, background = model_batch(env)
    edit = env.client.edit_image
    model = "seedream-5.5-image-to-image"

    def edit_image(prompt, refs, *, size, model):
        edit(prompt, refs, size=size, model=model)
        return png_bytes(768, 1024) if model.startswith("seedream") else png_bytes(1088, 1360)

    env.client.edit_image = edit_image
    run_background(background)
    crop_job = BackgroundTasks()
    pending = routes.retry_original_replacement_candidate(env.task_id, task["id"], model, env.request, crop_job, for_crop=True)
    assert pending["candidates"][1]["status"] == "queued"
    assert pending["candidates"][1]["for_crop"] is True
    run_background(crop_job)

    restored = routes.list_original_replacements(env.task_id, env.request)[0]
    candidate = restored["candidates"][1]
    assert candidate["for_crop"] is True and candidate["status"] == "succeeded"
    assert (candidate["output"]["width"], candidate["output"]["height"]) == (768, 1024)
    assert [call["model"] for call in env.client.edit_calls] == ["minimax-h3-image-edit", model]
    source = routes.get_original_replacement_crop_source(env.task_id, task["id"], model, env.request)
    assert source.media_type == "image/png"
    with Image.open(BytesIO(source.body)) as image:
        assert image.size == (768, 1024)
    with pytest.raises(HTTPException) as cannot_apply:
        apply(env, task, candidate_model=model)
    assert cannot_apply.value.status_code == 409
    assert env.db.whole_tasks.find_one({"_id": env.task_id})["current_version_id"] == env.source_id


@pytest.mark.parametrize("env", ["4:5"], indirect=True)
def test_crop_request_cannot_take_over_a_batch_before_its_worker_starts(env):
    task, background = model_batch(env)
    crop_job = BackgroundTasks()
    with pytest.raises(HTTPException) as queued:
        routes.retry_original_replacement_candidate(env.task_id, task["id"], "seedream-5.5-image-to-image",
                                                    env.request, crop_job, for_crop=True)
    assert queued.value.status_code == 409
    assert not crop_job.tasks
    run_background(background)
    assert [call["model"] for call in env.client.edit_calls] == ["minimax-h3-image-edit"]


def test_model_feedback_uses_original_source_and_applies_chosen_candidate(env):
    task, background = model_batch(env, candidate_model="minimax-h3-image-edit")
    run_background(background)
    chosen_model = "minimax-h3-image-edit"
    applied = apply(env, task, candidate_model=chosen_model)
    assert applied["current_version_id"] != env.source_id
    version = env.db.whole_versions.find_one({"_id": applied["current_version_id"]})
    assert version["parent_version_id"] == env.source_id
    assert version["model_input"]["model"] == chosen_model
    assert apply(env, task, candidate_model=chosen_model)["current_version_id"] == version["_id"]
    background = BackgroundTasks()
    rerun = routes.retry_model_replacement(env.task_id, task["id"],
        schemas.WholeImageOriginalReplacementFeedbackRequest(feedback="羊肉需要恢复到底稿大小"),
        env.request, background)
    assert rerun["source_version_id"] == env.source_id
    assert rerun["source_output_id"] == env.output_id
    assert rerun["retry_of"] == task["id"]
    run_background(background)
    assert len(routes.list_original_replacements(env.task_id, env.request)) == 2
    # The rerun keeps the model the operator chose instead of re-requesting all three.
    assert [call["model"] for call in env.client.edit_calls] == [chosen_model, chosen_model]
    assert rerun["candidate_model"] == chosen_model
    first, second = env.client.edit_calls
    assert first["reference_bytes"] == second["reference_bytes"]
    assert "羊肉需要恢复到底稿大小" in second["prompt"]
    assert "底稿上的 Logo" in second["prompt"]


@pytest.mark.parametrize("sibling_running", [False, True])
def test_retry_only_queues_the_failed_candidate_in_the_same_batch(env, sibling_running):
    failed_model = "seedream-5.5-image-to-image"
    task, background = model_batch(env, candidate_model=failed_model)
    edit = env.client.edit_image

    def fail_one(prompt, refs, *, size, model):
        if model == failed_model:
            raise RuntimeError("Image fetch failed")
        return edit(prompt, refs, size=size, model=model)

    env.client.edit_image = fail_one
    run_background(background)
    batches = env.db.background_replace_tasks
    baseline = batches.find_one({"_id": task["id"]})
    if sibling_running:
        batches.update_one({"_id": task["id"]}, {"$set": {"status": "running", "candidates.0.status": "running"}})
    env.client.edit_image = edit
    env.client.edit_calls.clear()
    retry = BackgroundTasks()
    response = routes.retry_original_replacement_candidate(env.task_id, task["id"], failed_model, env.request, retry)
    assert response["id"] == task["id"]
    assert response["status"] == "running"
    assert response["candidates"][1]["status"] == "queued"
    with pytest.raises(HTTPException) as duplicate:
        routes.retry_original_replacement_candidate(env.task_id, task["id"], failed_model, env.request, BackgroundTasks())
    assert duplicate.value.status_code == 409

    run_background(retry)
    assert [call["model"] for call in env.client.edit_calls] == [failed_model]
    assert env.client.edit_calls[0]["reference_names"][1:] == ["beef.png"]
    assert env.client.edit_calls[0]["prompt"] == baseline["candidates"][1]["model_input"]["prompt"]
    restored = batches.find_one({"_id": task["id"]})
    assert restored["status"] == ("running" if sibling_running else "succeeded")
    assert restored["candidates"][1]["status"] == "succeeded"
    assert restored["candidates"][2] == baseline["candidates"][2]
    assert batches.count_documents({}) == 1
    with pytest.raises(HTTPException) as already_succeeded:
        routes.retry_original_replacement_candidate(env.task_id, task["id"], failed_model, env.request, BackgroundTasks())
    assert already_succeeded.value.status_code == 409


def test_retry_recovers_an_interrupted_candidate_in_a_historical_batch(env):
    task, _ = model_batch(env)
    batches = env.db.background_replace_tasks
    batches.update_one({"_id": task["id"]}, {"$set": {"status": "interrupted", "candidates.1.status": "running"}})
    before = routes.list_original_replacements(env.task_id, env.request)[0]
    assert [candidate["status"] for candidate in before["candidates"]] == ["failed", "failed", "idle"]

    model = before["candidates"][1]["model"]
    retry = BackgroundTasks()
    routes.retry_original_replacement_candidate(env.task_id, task["id"], model, env.request, retry)
    run_background(retry)

    restored = routes.list_original_replacements(env.task_id, env.request)[0]
    assert restored["status"] == "succeeded"
    assert [candidate["status"] for candidate in restored["candidates"]] == ["failed", "succeeded", "idle"]
    assert [call["model"] for call in env.client.edit_calls] == [model]


def test_successful_candidates_apply_independently_and_keep_legacy_versions(env):
    task, background = model_batch(env)
    run_background(background)
    first_model = "seedream-5-pro-image-to-image"
    second_model = "minimax-h3-image-edit"
    on_demand = BackgroundTasks()
    routes.retry_original_replacement_candidate(env.task_id, task["id"], second_model, env.request, on_demand)
    run_background(on_demand)
    first_id = apply(env, task, candidate_model=first_model)["current_version_id"]
    # Reproduce a batch that was applied before per-candidate application was supported.
    env.db.whole_tasks.update_one({"_id": env.task_id}, {"$set": {"original_replacement_versions": {task["id"]: first_id}}})
    env.db.background_replace_tasks.update_one({"_id": task["id"]},
        {"$unset": {"candidates.0.applied_version_id": "", "candidates.0.applied_version": ""}})
    second_id = apply(env, task, candidate_model=second_model)["current_version_id"]
    assert first_id != second_id
    assert apply(env, task, candidate_model=first_model)["current_version_id"] == second_id
    assert apply(env, task, candidate_model=second_model)["current_version_id"] == second_id
    assert len(env.client.edit_calls) == 2
    versions = {doc["_id"]: doc for doc in env.db.whole_versions.find({"task_id": env.task_id})}
    assert len(versions) == 3
    assert versions[first_id]["parent_version_id"] == versions[second_id]["parent_version_id"] == env.source_id
    record = routes.list_original_replacements(env.task_id, env.request)[0]
    candidates = {item["model"]: item for item in record["candidates"]}
    assert candidates[first_model]["applied_version_id"] == first_id
    assert candidates[second_model]["applied_version_id"] == second_id
    assert record["applied_version_id"] == second_id


def test_model_candidate_cannot_be_applied_to_a_deleted_poster(env):
    task, background = model_batch(env, candidate_model="minimax-h3-image-edit")
    run_background(background)
    env.db.whole_tasks.update_one({"_id": env.task_id}, {"$set": {"candidate_deleted_at": whole_image.utcnow()}})
    with pytest.raises(HTTPException) as conflict:
        apply(env, task, candidate_model="minimax-h3-image-edit")
    assert conflict.value.status_code == 409
    assert env.db.whole_tasks.find_one({"_id": env.task_id})["current_version_id"] == env.source_id


def test_model_candidate_with_different_aspect_ratio_is_not_letterboxed(env):
    task, background = model_batch(env, candidate_model="minimax-h3-image-edit")
    edit = env.client.edit_image

    def edit_image(prompt, refs, *, size, model):
        if model == "minimax-h3-image-edit":
            return png_bytes(720, 1280)
        return edit(prompt, refs, size=size, model=model)

    env.client.edit_image = edit_image
    run_background(background)
    restored = routes.list_original_replacements(env.task_id, env.request)[0]
    assert restored["status"] == "failed"
    candidate = next(item for item in restored["candidates"] if item["model"] == "minimax-h3-image-edit")
    assert candidate["status"] == "failed"
    assert candidate["output"] is None
    assert "画布比例" in candidate["error"]


def test_original_replacement_routes_exist():
    paths = app.openapi()["paths"]
    assert "/api/merchants/{merchant_id}/whole-image/tasks/{task_id}/original-replacements" in paths
    assert "/api/merchants/{merchant_id}/whole-image/tasks/{task_id}/original-replacements/{replacement_id}/apply" in paths
    assert "/api/merchants/{merchant_id}/whole-image/tasks/{task_id}/original-replacements/{replacement_id}/refresh-result" in paths
    assert "/api/merchants/{merchant_id}/whole-image/tasks/{task_id}/original-replacements/{replacement_id}/download" in paths

def editor_files(env):
    env.client.artifacts = [
        AgentArtifact('editor.html', 'text/html', 'https://example.com/editor.html', '原照编辑器'),
        AgentArtifact('result.zip', 'application/zip', 'https://example.com/result.zip', '完整结果包'),
    ]


def test_replacement_keeps_html_bundle_preview_and_applied_version(env):
    editor_files(env)
    task = create(env)
    complete(env, task)
    restored = routes.list_original_replacements(env.task_id, env.request)[0]
    assert env.client.file_polls == [True]
    assert [item['kind'] for item in restored['artifacts']] == ['html', 'bundle']
    html = restored['artifacts'][0]
    assert html['preview_url'] == f"/api/whole-image/artifacts/{html['id']}/preview"
    preview = routes.preview_artifact(html['id'], env.request)
    assert b'Editor' in preview.body
    assert 'sandbox allow-scripts allow-downloads;' in preview.headers['content-security-policy']
    assert 'allow-same-origin' not in preview.headers['content-security-policy']
    assert routes.download_artifact(html['id'], env.request).body == preview.body
    result = apply(env, task)
    version = env.db.whole_versions.find_one({'_id': result['current_version_id']})
    assert [item['kind'] for item in version['outputs'][0]['artifacts']] == ['html', 'bundle']


def test_restore_html_from_existing_successful_run_without_regenerating(env):
    task = create(env)
    complete(env, task)
    before = env.db.background_replace_tasks.find_one({'_id': task['id']})
    editor_files(env)
    restored = routes.restore_original_replacement_artifacts(env.task_id, task['id'], env.request)
    again = routes.restore_original_replacement_artifacts(env.task_id, task['id'], env.request)
    assert restored['artifacts'] == again['artifacts']
    assert len(env.client.triggers) == 1
    assert len(env.client.file_downloads) == 2
    assert env.db.whole_artifacts.count_documents({}) == 2
    after = env.db.background_replace_tasks.find_one({'_id': task['id']})
    assert after['output'] == before['output']
    assert after['status'] == before['status'] == 'succeeded'
    assert env.db.whole_tasks.find_one({'_id': env.task_id})['current_version_id'] == env.source_id
    env.db.whole_tasks.insert_one({'_id': 'other-task'})
    with pytest.raises(HTTPException) as exc:
        routes.restore_original_replacement_artifacts('other-task', task['id'], env.request)
    assert exc.value.status_code == 404


def test_artifact_failure_keeps_png_success_and_can_be_retried(env):
    editor_files(env)
    env.client.file_error = True
    task = create(env)
    complete(env, task)
    restored = routes.list_original_replacements(env.task_id, env.request)[0]
    assert restored['status'] == 'succeeded'
    assert restored['output']
    assert restored['artifacts'] == []
    assert restored['artifact_error']
    env.client.file_error = False
    retried = routes.restore_original_replacement_artifacts(env.task_id, task['id'], env.request)
    assert len(retried['artifacts']) == 2
    assert retried['artifact_error'] is None

def test_restore_files_is_merchant_scoped_and_waits_for_known_run(env):
    from app.services.scoped_db import ScopedDatabase

    task = create(env)
    with pytest.raises(HTTPException) as exc:
        routes.restore_original_replacement_artifacts(env.task_id, task['id'], env.request)
    assert exc.value.status_code == 409
    assert env.client.triggers == []
    assert env.client.polls == []
    env.db.whole_tasks.update_one({'_id': env.task_id}, {'$set': {'merchant_id': 'm1'}})
    env.db.background_replace_tasks.update_one({'_id': task['id']}, {'$set': {'merchant_id': 'm1'}})
    env.request.state = SimpleNamespace(db=ScopedDatabase(env.db, 'm2'), store=env.store)
    with pytest.raises(HTTPException) as exc:
        routes.restore_original_replacement_artifacts(env.task_id, task['id'], env.request)
    assert exc.value.status_code == 404
    assert env.client.polls == []


def test_linked_replacement_refresh_reuses_run_and_scopes_to_parent(env):
    task = create(env)
    env.db.background_replace_tasks.update_one({"_id": task["id"]}, {"$set": {
        "status": "failed", "run_id": "existing-run"}})
    background = BackgroundTasks()
    refreshed = routes.refresh_original_replacement(env.task_id, task["id"], env.request, background)
    assert refreshed["status"] == "queued"
    assert refreshed["run_id"] == "existing-run"
    assert len(background.tasks) == 1
    assert env.client.triggers == []
    env.db.whole_tasks.insert_one({"_id": "another-poster"})
    with pytest.raises(HTTPException) as exc:
        routes.refresh_original_replacement("another-poster", task["id"], env.request, BackgroundTasks())
    assert exc.value.status_code == 404


def test_linked_replacement_download_remains_available(env):
    from fastapi.responses import FileResponse

    task = create(env)
    with pytest.raises(HTTPException) as exc:
        routes.download_original_replacement(env.task_id, task["id"], env.request)
    assert exc.value.status_code == 404
    complete(env, task)
    restored = routes.list_original_replacements(env.task_id, env.request)[0]
    assert restored["output"]["download_url"] == (
        f"/api/whole-image/tasks/{env.task_id}/original-replacements/{task['id']}/download")
    response = routes.download_original_replacement(env.task_id, task["id"], env.request)
    assert isinstance(response, FileResponse)
    assert response.media_type == "image/png"
    env.db.whole_tasks.insert_one({"_id": "another-poster"})
    with pytest.raises(HTTPException) as exc:
        routes.download_original_replacement("another-poster", task["id"], env.request)
    assert exc.value.status_code == 404


def test_replacement_uses_selected_output_and_only_selected_material(env):
    task = create(env)
    assert task["whole_task_id"] == env.task_id
    assert task["source_version_id"] == env.source_id
    assert task["source_output_id"] == env.output_id
    assert task["source_version"] == 1
    source = env.db.whole_versions.find_one({"_id": env.source_id})
    assert task["source"]["asset_id"] == source["output_asset_id"]
    complete(env, task)
    assert "右下角牛肉" in env.client.triggers[0][1]
    assert "environment" not in str(env.db.background_replace_tasks.find_one({"_id": task["id"]})["input"])
    assert len(env.client.uploads) == 2
    assert env.db.whole_tasks.find_one({"_id": env.task_id})["current_version_id"] == env.source_id
    assert env.db.whole_versions.count_documents({"task_id": env.task_id}) == 1
    restored = routes.list_original_replacements(env.task_id, env.request)
    assert [item["id"] for item in restored] == [task["id"]]
    assert restored[0]["status"] == "succeeded"


def test_logo_replacement_reaches_agent_and_saved_version_without_position(env):
    items = [{"target": "Logo", "asset_id": "logo"}, {"target": "右下角牛肉", "asset_id": "beef"}]
    task = create(env, items)
    complete(env, task)
    prompt = env.client.triggers[0][1]
    assert prompt.startswith("帮我把图1的Logo替换成图2，并且把图1的右下角牛肉替换成图3")
    assert "图2的 Logo 原文件" in prompt
    assert "独立可替换图层" in prompt
    assert "role=logo" in prompt
    assert "更换 Logo" in prompt
    assert "不与文字、笔刷或固定设计层合并" in prompt
    assert "https://example.com/input-1.png\nhttps://example.com/input-2.png\nhttps://example.com/input-3.png" in prompt
    result = apply(env, task)
    version = env.db.whole_versions.find_one({"_id": result["current_version_id"]})
    assert version["replacements"] == items


def test_split_all_elements_reaches_agent_and_survives_history_and_apply(env):
    task = create(env, [{"asset_id": "beef"}], split_all_elements=True)
    assert task["split_all_elements"] is True
    saved = env.db.background_replace_tasks.find_one({"_id": task["id"]})
    assert saved["input"]["split_all_elements"] is True
    complete(env, task)
    prompt = env.client.triggers[0][1]
    assert "自行识别图1中对应的替换对象和位置并替换" in prompt
    assert "全部设计元素" in prompt
    assert "标题、所有文字组、Logo、笔刷、贴纸、插画、装饰、分隔线和照片" in prompt
    assert "独立艺术字透明 PNG 图层" in prompt
    assert "不改写文案" in prompt
    assert "离线 HTML、完整图层" in prompt
    assert "与图1同尺寸的不透明最终 PNG" in prompt
    assert routes.list_original_replacements(env.task_id, env.request)[0]["split_all_elements"] is True
    result = apply(env, task)
    version = env.db.whole_versions.find_one({"_id": result["current_version_id"]})
    assert version["model_input"]["parameters"]["split_all_elements"] is True
    assert "全部拆分元素" in version["instruction"]


@pytest.mark.parametrize("options", [{}, {"split_all_elements": False}])
def test_split_all_elements_defaults_to_off_and_keeps_the_original_prompt(env, options):
    task = create(env, **options)
    assert task["split_all_elements"] is False
    complete(env, task)
    assert env.client.triggers[0][1] == (
        "帮我把图1的右下角牛肉替换成图2\n\n"
        "https://example.com/input-1.png\nhttps://example.com/input-2.png"
    )
    env.db.background_replace_tasks.update_one({"_id": task["id"]}, {"$unset": {"input.split_all_elements": ""}})
    assert routes.list_original_replacements(env.task_id, env.request)[0]["split_all_elements"] is False


@pytest.mark.parametrize("target", [None, "", "   "])
def test_optional_position_reaches_agent_and_saved_version(env, target):
    photo = {"asset_id": "beef"}
    if target is not None:
        photo["target"] = target
    task = create(env, [photo])
    complete(env, task)
    assert env.client.triggers[0][1] == (
        "帮我根据图2的原照内容，自行识别图1中对应的替换对象和位置并替换\n\n"
        "https://example.com/input-1.png\nhttps://example.com/input-2.png"
    )
    restored = routes.list_original_replacements(env.task_id, env.request)[0]
    assert restored["replacements"][0]["target"] == ""
    result = apply(env, task)
    version = env.db.whole_versions.find_one({"_id": result["current_version_id"]})
    assert version["replacements"] == [{"target": "", "asset_id": "beef"}]
    assert version["instruction"] == "原照替换：自动识别"
    assert version["model_input"]["images"][1]["role"] == "自动识别"


@pytest.mark.parametrize("items", [[], [{"target": "牛肉", "asset_id": " "}],
    [{"target": "牛" * 1001, "asset_id": "beef"}],
    [{"target": "牛肉", "asset_id": "beef"}] * 6])
def test_invalid_replacement_selection_is_rejected(env, items):
    with pytest.raises(ValidationError):
        create(env, items)
    assert env.db.background_replace_tasks.count_documents({}) == 0


def test_same_original_can_replace_multiple_targets(env):
    task = create(env, [{"target": "左边牛肉", "asset_id": "beef"},
                        {"target": "右边牛肉", "asset_id": "beef"}])
    complete(env, task)
    assert "左边牛肉替换成图2，并且把图1的右边牛肉替换成图2" in env.client.triggers[0][1]


@pytest.mark.parametrize("overrides", [{"source_version_id": "missing"}, {"source_output_id": "missing"}])
def test_invalid_source_does_not_create_a_remote_task(env, overrides):
    with pytest.raises(HTTPException) as exc:
        create(env, **overrides)
    assert exc.value.status_code == 422
    assert env.db.background_replace_tasks.count_documents({}) == 0


def test_transparent_source_and_missing_material_are_rejected(env):
    env.db.whole_versions.update_one({"_id": env.source_id}, {"$set": {"outputs.0.selectable": False}})
    with pytest.raises(HTTPException) as exc:
        create(env)
    assert exc.value.status_code == 422
    env.db.whole_versions.update_one({"_id": env.source_id}, {"$set": {"outputs.0.selectable": True}})
    with pytest.raises(HTTPException):
        create(env, [{"target": "牛肉", "asset_id": "missing"}])
    assert env.db.background_replace_tasks.count_documents({}) == 0


def test_apply_adds_one_version_and_reuses_the_saved_asset(env):
    task = create(env)
    finished = complete(env, task)
    output_id = finished["output"]["asset_id"]
    image = env.db.assets.find_one({"_id": output_id})
    before = env.store.get_bytes(image["path"])
    result = apply(env, task)
    assert result["current_version"] == 2
    version = env.db.whole_versions.find_one({"_id": result["current_version_id"]})
    assert version["operation"] == "original_replace"
    assert version["parent_version_id"] == env.source_id
    assert version["source_output_id"] == env.output_id
    assert version["replacement_task_id"] == task["id"]
    assert version["replacements"] == [{"target": "右下角牛肉", "asset_id": "beef"}]
    assert version["output_asset_id"] == output_id
    assert result["outputs"][0]["role"] == "original_replace_result"
    assert result["edit"]["branches"] == {}
    assert env.store.get_bytes(image["path"]) == before
    assert len(env.client.triggers) == len(env.client.downloads) == 1
    versions = routes.list_versions(env.task_id, env.request)
    assert versions[-1]["replacement_task_id"] == task["id"]
    restored = routes.list_original_replacements(env.task_id, env.request)[0]
    assert restored["applied_version_id"] == version["_id"]
    whole_image.select_version(env.db, env.task_id, env.source_id)
    again = apply(env, task)
    assert again["current_version_id"] == env.source_id
    assert env.db.whole_versions.count_documents({"task_id": env.task_id}) == 2


def test_apply_preserves_old_source_after_newer_edits(env):
    task = create(env)
    complete(env, task)
    newer = env.db.whole_versions.find_one({"_id": env.source_id})
    newer.update(_id="newer", version=2, operation="edit")
    env.db.whole_versions.insert_one(newer)
    whole_image.select_version(env.db, env.task_id, "newer")
    with pytest.raises(HTTPException) as exc:
        apply(env, task, expected_current_version_id=env.source_id)
    assert exc.value.status_code == 409
    result = apply(env, task)
    assert result["current_version"] == 3
    version = env.db.whole_versions.find_one({"_id": result["current_version_id"]})
    assert version["parent_version_id"] == env.source_id
    assert env.db.whole_versions.find_one({"_id": "newer"})


@pytest.mark.parametrize("state", ["queued", "failed", "partial"])
def test_unfinished_or_ambiguous_result_cannot_be_applied(env, state):
    task = create(env)
    env.db.background_replace_tasks.update_one({"_id": task["id"]}, {"$set": {"status": state}})
    with pytest.raises(HTTPException) as exc:
        apply(env, task)
    assert exc.value.status_code == 409
    assert env.db.whole_versions.count_documents({"task_id": env.task_id}) == 1


def test_apply_during_edit_does_not_overwrite_it(env):
    task = create(env)
    complete(env, task)
    env.db.whole_tasks.update_one({"_id": env.task_id}, {"$set": {"edit.status": "running"}})
    with pytest.raises(HTTPException) as exc:
        apply(env, task)
    assert exc.value.status_code == 409
    assert env.db.whole_versions.count_documents({"task_id": env.task_id}) == 1


def test_other_tasks_cannot_apply_the_result(env):
    task = create(env)
    complete(env, task)
    env.db.background_replace_tasks.update_one({"_id": task["id"]}, {"$set": {"whole_task_id": "other"}})
    with pytest.raises(HTTPException) as exc:
        apply(env, task)
    assert exc.value.status_code == 404


def test_apply_failure_does_not_delete_shared_image(env, monkeypatch):
    task = create(env)
    finished = complete(env, task)
    original_update = env.db.whole_tasks.update_one

    def fail_commit(query, update, *args, **kwargs):
        if update.get("$set", {}).get("current_version_id"):
            raise RuntimeError("database unavailable")
        return original_update(query, update, *args, **kwargs)

    monkeypatch.setattr(env.db.whole_tasks, "update_one", fail_commit)
    with pytest.raises(RuntimeError, match="database unavailable"):
        apply(env, task)
    image = env.db.assets.find_one({"_id": finished["output"]["asset_id"]})
    assert image and env.store.get_bytes(image["path"])
    assert env.db.whole_versions.count_documents({"task_id": env.task_id}) == 1
    assert env.db.whole_tasks.find_one({"_id": env.task_id})["edit"]["status"] == "idle"
    monkeypatch.setattr(env.db.whole_tasks, "update_one", original_update)
    assert apply(env, task)["current_version"] == 2


def test_task_center_links_associated_replacements_to_the_whole_task(env):
    first = create(env)
    complete(env, first)
    second = create(env)
    for kind in ("all", "original_replace"):
        result = task_center.list_task_center(env.request, kind=kind, status="active")
        assert result["total"] == 1
        group = result["items"][0]
        assert group["id"] == env.task_id
        assert group["kind"] == "generate"
        assert group["candidate_count"] == 1
        assert group["original_replacement"]["id"] == second["id"]
        assert group["original_replacement"]["whole_task_id"] == env.task_id
    finished = task_center.list_task_center(env.request, kind="original_replace", status="ready")["items"][0]
    assert finished["id"] == env.task_id
    assert finished["original_replacement"]["id"] == first["id"]
    assert finished["original_replacement"]["download_url"]


def test_restart_does_not_resume_retired_standalone_tasks(env):
    env.db.background_replace_tasks.insert_one({
        "_id": "retired-task", "status": "running", "run_id": "retired-run"})
    assert original_replacement_runner.mark_interrupted(env.db) == 0
    original_replacement_runner.resume_tasks(env.db, env.settings, env.store, env.client)
    assert env.client.triggers == []
    assert env.client.polls == []


def test_only_linked_original_replacements_protect_materials():
    from app.services.asset_records import asset_referenced

    db = mongomock.MongoClient().db
    db.background_replace_tasks.insert_one({"_id": "archived", "input": {
        "source_asset_id": "archived-source", "replacements": [{"asset_id": "shared-material"}]}})
    assert not asset_referenced(db, "archived-source")
    assert not asset_referenced(db, "shared-material")
    db.background_replace_tasks.insert_one({"_id": "linked", "whole_task_id": "poster", "input": {
        "source_asset_id": "linked-source", "replacements": [{"asset_id": "shared-material"}]}})
    assert asset_referenced(db, "linked-source")
    assert asset_referenced(db, "shared-material")


def test_version_preparation_failure_releases_the_apply_lock(env, monkeypatch):
    task = create(env)
    complete(env, task)
    with monkeypatch.context() as patch:
        patch.setattr(whole_image, "_next_version", lambda *_: (_ for _ in ()).throw(RuntimeError("read unavailable")))
        with pytest.raises(RuntimeError, match="read unavailable"):
            apply(env, task)
    assert env.db.whole_tasks.find_one({"_id": env.task_id})["edit"]["status"] == "idle"
    assert apply(env, task)["current_version"] == 2


def test_concurrent_retry_cannot_mark_an_uncommitted_version_as_applied(env, monkeypatch):
    task = create(env)
    complete(env, task)
    original_insert = env.db.whole_versions.insert_one
    retried = []

    def insert_then_retry(version, *args, **kwargs):
        result = original_insert(version, *args, **kwargs)
        try:
            apply(env, task)
            retried.append("returned")
        except HTTPException as exc:
            retried.append(exc.status_code)
        return result

    original_update = env.db.whole_tasks.update_one

    def fail_commit(query, update, *args, **kwargs):
        if update.get("$set", {}).get("current_version_id"):
            raise RuntimeError("commit unavailable")
        return original_update(query, update, *args, **kwargs)

    monkeypatch.setattr(env.db.whole_versions, "insert_one", insert_then_retry)
    monkeypatch.setattr(env.db.whole_tasks, "update_one", fail_commit)
    with pytest.raises(RuntimeError, match="commit unavailable"):
        apply(env, task)
    assert retried == [409]
    assert not original_replacement_runner.get_task(env.db, task["id"]).get("applied_version_id")
    assert env.db.whole_versions.count_documents({"task_id": env.task_id}) == 1


def test_linked_replacement_resumes_existing_run_after_restart(env):
    task = create(env)
    now = whole_image.utcnow()
    env.db.background_replace_tasks.update_one({"_id": task["id"]}, {"$set": {
        "status": "running", "run_id": "saved-run", "started_at": now, "deadline_at": now + timedelta(minutes=45)}})
    whole_image.mark_interrupted(env.db)
    assert original_replacement_runner.mark_interrupted(env.db) == 1
    original_replacement_runner.resume_tasks(env.db, env.settings, env.store, env.client)
    restored = routes.list_original_replacements(env.task_id, env.request)[0]
    assert restored["status"] == "succeeded"
    assert restored["source_version_id"] == env.source_id
    assert env.client.triggers == []
    assert env.client.polls == ["saved-run"]
    assert env.db.whole_tasks.find_one({"_id": env.task_id})["current_version_id"] == env.source_id


def test_normal_edit_of_applied_result_does_not_inherit_replacement_metadata(env):
    task = create(env)
    complete(env, task)
    applied = apply(env, task)
    client = FakeCoreAI()
    request = _request(env.db, env.settings, env.store, client)
    payload = schemas.WholeImageEditRequest(instruction="修改标题", source_version_id=applied["current_version_id"],
                                           source_output_id=applied["current_output_id"])
    routes.create_edit(env.task_id, payload, request, BackgroundTasks())
    whole_image.run_edit(env.db, env.settings, env.store, client, env.task_id,
                         payload.instruction, payload.source_version_id, payload.source_output_id)
    latest = routes.list_versions(env.task_id, request)[-1]
    assert latest["operation"] == "edit"
    assert latest["replacement_task_id"] is None
    assert latest["replacements"] == []
    assert latest["parent_version_id"] == applied["current_version_id"]
    assert all(item["role"] != "original_replace_result" for item in latest["outputs"])


def test_metadata_failure_after_commit_can_be_retried_without_reverting_selection(env, monkeypatch):
    task = create(env)
    complete(env, task)
    original_update = env.db.background_replace_tasks.update_one

    def fail_metadata(query, update, *args, **kwargs):
        if "applied_version_id" in update.get("$set", {}):
            raise RuntimeError("metadata unavailable")
        return original_update(query, update, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(env.db.background_replace_tasks, "update_one", fail_metadata)
        with pytest.raises(RuntimeError, match="metadata unavailable"):
            apply(env, task)
    whole_image.select_version(env.db, env.task_id, env.source_id)
    assert apply(env, task)["current_version_id"] == env.source_id
    assert env.db.whole_versions.count_documents({"task_id": env.task_id}) == 2
    assert original_replacement_runner.get_task(env.db, task["id"])["applied_version_id"]


# ------------------------------------------------------- series: Logo stays backend-owned

def _as_series_page(env, *, logo_asset="logo"):
    env.db.whole_tasks.update_one({"_id": env.task_id}, {"$set": {"series": {
        "run_id": "run-1", "group_id": "grp-1", "page_id": "page-2", "order": 1,
        "role": "detail", "page_count": 2, "user_prompt": "系列海报",
        "logo_asset_id": logo_asset,
        "logo_box": {"x": 40, "y": 40, "width": 200, "height": 70},
    }}})


@pytest.mark.parametrize("logo_asset", ["logo", None])
def test_series_photo_replacement_sends_the_official_logo_to_the_agent_when_enabled(env, logo_asset):
    _as_series_page(env, logo_asset="environment")
    env.db.whole_series_groups.insert_one({"_id": "grp-1", "logo_asset_id": logo_asset})
    logo = storage.prepare_upload(env.settings, env.store, png_bytes(32, 32), "official.png")
    env.db.assets.update_one({"_id": "logo"}, {"$set": {
        "path": logo.path, "prepared_path": logo.prepared_path,
    }})
    # Legacy page metadata can be stale. The group's explicit Logo choice wins.
    task = create(env)
    stored = complete(env, task)
    _, text, _ = env.client.triggers[0]
    assert stored["input"]["replacements"] == [{"target": "右下角牛肉", "asset_id": "beef"}]
    if logo_asset:
        assert text.endswith("https://example.com/input-3.png")
        assert "图3是官方 Logo" in text
        assert env.client.uploads[-1] == env.store.get_bytes(logo.path)
        assert task["logo_reference"]["asset_id"] == logo_asset
    else:
        assert len(env.client.uploads) == 2
        assert task.get("logo_reference") is None
    assert stored["agent_input"] == text


def test_series_model_replacement_sends_the_official_logo_with_the_photo(env):
    _as_series_page(env)
    task, background = model_batch(env)
    run_background(background)
    assert task["logo_reference"]["asset_id"] == "logo"
    assert len(env.client.edit_calls) == 1
    for call in env.client.edit_calls:
        assert call["reference_names"][1:] == ["beef.png", "logo.png"]
        assert "图片 3 是官方 Logo" in call["prompt"]


def test_series_page_rejects_a_logo_replacement_request(env):
    _as_series_page(env)
    with pytest.raises(HTTPException) as error:
        create(env, [{"target": "Logo", "asset_id": "logo"}])
    assert error.value.status_code == 422
    assert "仅替换照片" in str(error.value.detail)
    # Naming the official Logo asset is a Logo request even under a photo-like target.
    with pytest.raises(HTTPException) as error:
        create(env, [{"target": "右下角牛肉", "asset_id": "logo"}])
    assert error.value.status_code == 422
    assert env.db.background_replace_tasks.count_documents({}) == 0
    # A photo-only record is still accepted on the same series page.
    task = create(env, [{"target": "右下角牛肉", "asset_id": "beef"}])
    stored = env.db.background_replace_tasks.find_one({"_id": task["id"]})
    assert stored["input"]["replacements"] == [{"target": "右下角牛肉", "asset_id": "beef"}]


def test_series_page_blocks_apply_of_a_logo_requesting_record(env):
    from app.services import original_replacement
    from app.services.whole_image import StudioError

    _as_series_page(env)
    env.db.background_replace_tasks.insert_one({
        "_id": "brep_logo", "status": "succeeded", "stage": "done",
        "whole_task_id": env.task_id, "kind": "agent",
        "source_version_id": env.source_id, "source_output_id": env.output_id,
        "source": {"asset_id": "beef", "name": "底图", "width": 640, "height": 640},
        "input": {"replacements": [{"target": "Logo", "asset_id": "logo"}],
                  "source_asset_id": "beef", "split_all_elements": False},
        "replacements": [{"target": "Logo", "asset_id": "logo"}],
        "output": {"asset_id": "beef", "width": 640, "height": 640},
        "artifacts": [],
    })
    with pytest.raises(StudioError) as error:
        original_replacement.apply(
            env.db, env.task_id, "brep_logo",
            expected_current_version_id=env.source_id,
            expected_current_output_id=env.output_id,
            settings=env.settings, store=env.store,
        )
    assert error.value.status_code == 422
    assert "重新生成仅替换照片" in error.value.message
    # Nothing was published for the blocked record.
    current = env.db.whole_tasks.find_one({"_id": env.task_id})
    assert current["current_version_id"] == env.source_id


def test_single_poster_still_accepts_explicit_logo_replacement(env):
    task = create(env, [{"target": "Logo", "asset_id": "logo"},
                        {"target": "右下角牛肉", "asset_id": "beef"}])
    stored = env.db.background_replace_tasks.find_one({"_id": task["id"]})
    assert [item["target"] for item in stored["input"]["replacements"]] == ["Logo", "右下角牛肉"]


def _logo_task(env, client, target="Logo"):
    return model_batch(env, [{"target": "右下角牛肉", "asset_id": "beef"},
                             {"target": target, "asset_id": "logo"}], client=client)


def test_model_logo_overlay_uses_the_detected_box_and_erases_only_it(env):
    detected = {"found": True, "x": 0.05, "y": 0.06, "width": 0.2, "height": 0.1}
    env.client = LogoAwareCoreAI(detected)
    task, background = _logo_task(env, env.client)
    run_background(background)

    stored = env.db.background_replace_tasks.find_one({"_id": task["id"]})
    source_w, source_h = stored["source"]["width"], stored["source"]["height"]
    overlay = stored["logo_overlay"]
    assert overlay["box"]["detected"] is True
    assert overlay["box"]["x"] > 0 and overlay["box"]["y"] > 0
    assert overlay["box"]["x"] + overlay["box"]["width"] <= source_w
    assert overlay["box"]["y"] + overlay["box"]["height"] <= source_h
    assert (overlay["width"], overlay["height"]) == (320, 240)
    assert overlay["erase_box"] == {"x": round(0.05 * source_w), "y": round(0.06 * source_h),
                                    "width": round(0.2 * source_w), "height": round(0.1 * source_h)}
    # The official file keeps its own aspect ratio; only its scale follows the old frame.
    assert round(overlay["box"]["width"] / overlay["box"]["height"], 4) == round(320 / 240, 4)

    prompt = stored["candidates"][0]["model_input"]["prompt"]
    assert "移除图片 1 中原有的品牌 Logo/商标标识" in prompt
    assert "上 6%–16%" in prompt
    assert "不要在画面任何位置绘制任何品牌 Logo" in prompt
    assert "图片 2 是替换原照。目标：右下角牛肉。" in prompt
    assert [item["asset_id"] for item in stored["candidates"][0]["model_input"]["images"]] == [
        stored["source"]["asset_id"], "beef"]
    assert env.client.edit_calls[0]["reference_names"][1:] == ["beef.png"]
    assert env.client.chat_model == env.settings.asset_index_model

    task_out = routes.list_original_replacements(env.task_id, env.request)[0]
    assert task_out["logo_overlay"]["box"] == overlay["box"]
    assert task_out["logo_overlay"]["asset_id"] == "logo"
    assert task_out["logo_overlay"]["source_url"].endswith(
        f"/original-replacements/{task['id']}/logo-source")
    assert task_out["logo_overlay"]["preview_url"]

    logo_source = routes.get_original_replacement_logo_source(env.task_id, task["id"], env.request)
    assert logo_source.media_type == "image/png"
    with Image.open(BytesIO(logo_source.body)) as image:
        assert image.size == (320, 240)
        assert "A" in image.getbands()
    base_source = routes.get_original_replacement_crop_source(
        env.task_id, task["id"], "seedream-5-pro-image-to-image", env.request)
    assert base_source.media_type == "image/png"
    with Image.open(BytesIO(base_source.body)) as image:
        assert image.size == (source_w, source_h)
    assert task_out["candidates"][0]["base_source_url"].endswith("/crop-source")


def test_model_logo_only_batch_still_sends_the_base_poster(env):
    env.client = LogoAwareCoreAI({"found": False})
    task, background = model_batch(env, [{"target": "Logo", "asset_id": "logo"}], client=env.client)
    run_background(background)
    # Dropping the Logo asset must not leave an empty reference list behind.
    assert len(env.client.edit_calls[0]["reference_names"]) == 1
    stored = env.db.background_replace_tasks.find_one({"_id": task["id"]})
    sent = stored["candidates"][0]["model_input"]["images"]
    assert [item["asset_id"] for item in sent] == [stored["source"]["asset_id"]]
    assert [item["role"] for item in sent] == ["替换底图"]
    assert stored["status"] == "succeeded"
    assert stored["logo_overlay"]["asset_id"] == "logo"


@pytest.mark.parametrize("answer", [{"found": False}, RuntimeError("vision offline")])
def test_model_logo_overlay_falls_back_to_a_corner_box(env, answer):
    client = LogoAwareCoreAI(answer) if isinstance(answer, dict) else LogoAwareCoreAI(logo_error=answer)
    env.client = client
    task, background = _logo_task(env, client)
    run_background(background)

    stored = env.db.background_replace_tasks.find_one({"_id": task["id"]})
    assert stored["status"] == "succeeded"
    overlay = stored["logo_overlay"]
    assert overlay["box"]["detected"] is False
    assert overlay["erase_box"] is None
    margin = max(12, round(min(stored["source"]["width"], stored["source"]["height"]) * 0.045))
    assert (overlay["box"]["x"], overlay["box"]["y"]) == (margin, margin)
    assert overlay["box"]["x"] + overlay["box"]["width"] <= stored["source"]["width"]
    assert overlay["box"]["y"] + overlay["box"]["height"] <= stored["source"]["height"]
    prompt = stored["candidates"][0]["model_input"]["prompt"]
    assert "若图片 1 中存在原有的小型品牌 Logo/商标标识" in prompt
    assert "移除图片 1 中原有的品牌 Logo" not in prompt


@pytest.mark.parametrize("target,position", [("右上角商标", "top-right"), ("左下角标志", "bottom-left"),
                                             ("顶部居中标识", "top-center"), ("右下角 Logo", "bottom-right")])
def test_model_logo_overlay_places_the_fallback_box_by_the_target_hint(env, target, position):
    env.client = LogoAwareCoreAI({"found": False})
    task, background = _logo_task(env, env.client, target=target)
    run_background(background)
    stored = env.db.background_replace_tasks.find_one({"_id": task["id"]})
    overlay = stored["logo_overlay"]
    source_w, source_h = stored["source"]["width"], stored["source"]["height"]
    vertical, horizontal = position.split("-")
    if horizontal == "right":
        assert overlay["box"]["x"] + overlay["box"]["width"] > source_w / 2
    elif horizontal == "center":
        assert abs(overlay["box"]["x"] + overlay["box"]["width"] / 2 - source_w / 2) <= 1
    else:
        assert overlay["box"]["x"] < source_w / 2
    assert (overlay["box"]["y"] < source_h / 2) is (vertical == "top")


def _composited_png(width, height, color=(20, 140, 90)):
    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def test_apply_saves_the_client_composited_png_as_a_new_version(env):
    env.client = LogoAwareCoreAI({"found": True, "x": 0.05, "y": 0.06, "width": 0.2, "height": 0.1})
    task, background = _logo_task(env, env.client)
    run_background(background)
    model = "seedream-5-pro-image-to-image"
    first_box = {"x": 40, "y": 30, "width": 150, "height": 110}
    first_png = _composited_png(task["source"]["width"], task["source"]["height"])
    applied = apply(env, task, candidate_model=model, composited_png_base64=f"data:image/png;base64,{first_png}",
                    logo_box=first_box)
    version = env.db.whole_versions.find_one({"_id": applied["current_version_id"]})
    asset = env.db.assets.find_one({"_id": version["output_asset_id"]})
    assert (asset["width"], asset["height"]) == (task["source"]["width"], task["source"]["height"])
    stored = env.db.background_replace_tasks.find_one({"_id": task["id"]})
    assert stored["candidates"][0]["logo_box"] == first_box
    assert stored["candidates"][0]["composited_output"]["asset_id"] == version["output_asset_id"]
    # The candidate preview now shows the composited file instead of the raw model output.
    task_out = routes.list_original_replacements(env.task_id, env.request)[0]
    assert task_out["candidates"][0]["logo_box"] == first_box
    assert task_out["candidates"][0]["output"]["preview_url"].endswith(
        f"/assets/{version['output_asset_id']}/file")
    assert task_out["candidates"][0]["base_source_url"].endswith("/crop-source")

    # A tweaked box produces a second version instead of reusing the first one.
    second_box = {"x": 90, "y": 60, "width": 210, "height": 140}
    second_png = _composited_png(task["source"]["width"], task["source"]["height"], (10, 60, 200))
    current = env.db.whole_tasks.find_one({"_id": env.task_id})
    tweaked = apply(env, task, candidate_model=model, composited_png_base64=second_png, logo_box=second_box,
                    expected_current_version_id=current["current_version_id"],
                    expected_current_output_id=current["current_output_id"])
    assert tweaked["current_version_id"] != applied["current_version_id"]
    stored = env.db.background_replace_tasks.find_one({"_id": task["id"]})
    assert stored["candidates"][0]["logo_box"] == second_box
    assert stored["candidates"][0]["applied_version_id"] == tweaked["current_version_id"]
    assert env.db.whole_versions.count_documents({"task_id": env.task_id,
                                                "operation": "original_replace"}) == 2


def test_apply_rejects_a_broken_or_mismatched_composited_png(env):
    env.client = LogoAwareCoreAI({"found": False})
    task, background = _logo_task(env, env.client)
    run_background(background)
    model = "seedream-5-pro-image-to-image"
    with pytest.raises(HTTPException) as wrong_size:
        apply(env, task, candidate_model=model, composited_png_base64=_composited_png(120, 90),
              logo_box={"x": 0, "y": 0, "width": 10, "height": 10})
    assert wrong_size.value.status_code == 422
    assert wrong_size.value.detail == "合成图片格式或尺寸无效"
    with pytest.raises(HTTPException) as not_base64:
        apply(env, task, candidate_model=model, composited_png_base64="not-a-png")
    assert not_base64.value.status_code == 422
    with pytest.raises(HTTPException) as not_png:
        apply(env, task, candidate_model=model,
              composited_png_base64=base64.b64encode(b"GIF89a-not-a-png").decode("ascii"))
    assert not_png.value.status_code == 422
    assert env.db.whole_tasks.find_one({"_id": env.task_id})["current_version_id"] == env.source_id
    assert env.db.whole_versions.count_documents({"task_id": env.task_id,
                                                 "operation": "original_replace"}) == 0


def test_apply_keeps_a_negative_logo_box_off_the_canvas_edge(env):
    env.client = LogoAwareCoreAI({"found": False})
    task, background = _logo_task(env, env.client)
    run_background(background)
    model = "seedream-5-pro-image-to-image"
    # A Logo with transparent padding may legally hang past the left/top edge.
    box = {"x": -34, "y": -18, "width": 180, "height": 120}
    applied = apply(env, task, candidate_model=model,
                    composited_png_base64=_composited_png(task["source"]["width"], task["source"]["height"]),
                    logo_box=box)
    stored = env.db.background_replace_tasks.find_one({"_id": task["id"]})
    assert stored["candidates"][0]["logo_box"] == box
    assert stored["candidates"][0]["applied_version_id"] == applied["current_version_id"]
    task_out = routes.list_original_replacements(env.task_id, env.request)[0]
    # Reopening 调整 Logo must find the operator's placement again, not a clamped copy.
    assert task_out["candidates"][0]["logo_box"] == box


@pytest.mark.parametrize("box", [{"x": 12, "y": 9, "width": 0, "height": 40},
                                {"x": 12, "y": 9, "width": 40, "height": -5}])
def test_apply_rejects_a_logo_box_without_a_size(env, box):
    env.client = LogoAwareCoreAI({"found": False})
    task, background = _logo_task(env, env.client)
    run_background(background)
    with pytest.raises(HTTPException) as error:
        apply(env, task, candidate_model="seedream-5-pro-image-to-image",
              composited_png_base64=_composited_png(task["source"]["width"], task["source"]["height"]),
              logo_box=box)
    assert error.value.status_code == 422
    assert error.value.detail == "Logo 位置无效，请重新调整"


def test_feedback_rerun_reuses_the_logo_plan_instead_of_detecting_again(env):
    env.client = LogoAwareCoreAI({"found": True, "x": 0.05, "y": 0.06, "width": 0.2, "height": 0.1})
    task, background = _logo_task(env, env.client)
    run_background(background)
    assert env.client.chat_calls == 1
    overlay = env.db.background_replace_tasks.find_one({"_id": task["id"]})["logo_overlay"]

    rerun_background = BackgroundTasks()
    rerun = routes.retry_model_replacement(env.task_id, task["id"],
        schemas.WholeImageOriginalReplacementFeedbackRequest(feedback="Logo 再往右一些"),
        env.request, rerun_background)
    assert env.db.background_replace_tasks.find_one({"_id": rerun["id"]})["logo_overlay"] == overlay
    run_background(rerun_background)
    # The same base poster keeps its Logo plan, so the vision model is asked only once.
    assert env.client.chat_calls == 1
    stored = env.db.background_replace_tasks.find_one({"_id": rerun["id"]})
    assert stored["logo_overlay"] == overlay
    assert "移除图片 1 中原有的品牌 Logo/商标标识" in stored["candidates"][0]["model_input"]["prompt"]

