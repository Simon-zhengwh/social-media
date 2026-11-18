from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pymongo import ReturnDocument

from app.config import Settings
from app.services import original_replacement_runner, whole_image
from app.services.canvas import gateway_image_size
from app.services.core_ai import CoreAIClient
from app.services.image_ref import cleanup_image_refs, image_ref_from_asset
from app.services.object_store import ObjectStore
from app.services.whole_image import StudioError

def _task(db: Any, task_id: str) -> dict[str, Any]:
    task = db.whole_tasks.find_one({"_id": task_id})
    if task is None:
        raise StudioError("task_missing", "海报任务不存在", 404)
    return task


def create(db: Any, task_id: str, *, source_version_id: str, source_output_id: str,
           replacements: list[dict[str, str]], split_all_elements: bool = False, kind: str = "agent",
           retry_of: str | None = None, feedback: str = "") -> dict[str, Any]:
    task = _task(db, task_id)
    if task.get("status") != "succeeded":
        raise StudioError("source_not_ready", "请先生成并选择海报", 409)
    version = db.whole_versions.find_one({"_id": source_version_id, "task_id": task_id})
    if version is None:
        raise StudioError("invalid_source", "来源版本不属于当前海报", 422)
    output = next((item for item in whole_image._version_outputs(version)
                   if item.get("id") == source_output_id), None)
    if not output or not output.get("selectable"):
        raise StudioError("invalid_source", "请选择可编辑的完整海报结果，透明图层不能作为底图", 422)
    association = {"whole_task_id": task_id, "source_version_id": source_version_id,
                   "source_output_id": source_output_id, "source_version": version["version"]}
    if kind == "model":
        association.update(kind=kind, retry_of=retry_of, feedback=feedback)
    return original_replacement_runner.create_task(
        db, source_asset_id=output["asset_id"], whole_task_id=task_id, replacements=replacements,
        split_all_elements=kind == "agent" and split_all_elements, metadata=association)


def retry_model_batch(db: Any, task_id: str, replacement_id: str, feedback: str) -> dict[str, Any]:
    previous = get_task(db, task_id, replacement_id)
    if previous.get("kind") != "model" or previous["status"] in original_replacement_runner.ACTIVE:
        raise StudioError("batch_not_ready", "请等待本批模型处理结束", 409)
    return create(db, task_id, source_version_id=previous["source_version_id"],
                  source_output_id=previous["source_output_id"], replacements=previous["input"]["replacements"],
                  kind="model", retry_of=replacement_id, feedback=feedback)


def queue_model_candidate_retry(db: Any, task_id: str, replacement_id: str, model: str) -> dict[str, Any]:
    batch = get_task(db, task_id, replacement_id)
    candidates = batch.get("candidates", [])
    index = next((i for i, item in enumerate(candidates) if item["model"] == model), None)
    if batch.get("kind") != "model" or index is None:
        raise StudioError("candidate_missing", "找不到这张模型候选", 404)
    candidate = original_replacement_runner.model_candidate(model, batch["source"])
    if candidate["status"] == "unsupported":
        raise StudioError("unsupported_format", candidate["error"], 422)
    field = f"candidates.{index}"
    current = candidates[index]["status"]
    terminal_failure = batch["status"] in {"failed", "interrupted"}
    if current != "failed" and not (terminal_failure and current in original_replacement_runner.ACTIVE):
        raise StudioError("candidate_not_retryable", "只能重试失败的候选", 409)

    query = {"_id": replacement_id, f"{field}.status": current}
    values: dict[str, Any] = {"status": "running", "stage": "model_running", "progress": 0,
                              "error": None, "updated_at": whole_image.utcnow()}
    if terminal_failure:
        # Older failed or interrupted records may still have unfinished candidate states.
        query["status"] = batch["status"]
        error = (batch.get("error") or {}).get("message") or "模型结果未保存"
        for i, item in enumerate(candidates):
            if i != index and item["status"] in original_replacement_runner.ACTIVE:
                values[f"candidates.{i}.status"] = "failed"
                values[f"candidates.{i}.error"] = error
    values[field] = candidate
    claimed = db.background_replace_tasks.find_one_and_update(query, {"$set": values}, return_document=ReturnDocument.AFTER)
    if claimed is None:
        raise StudioError("candidate_changed", "候选状态已变化，请稍后重试", 409)
    return claimed


def _model_prompt(task: dict[str, Any]) -> str:
    lines = ["图片 1 是唯一的海报底稿。保持原画布比例、取景、标题、文字、排版、装饰和未指定区域。"
             "只对下列素材对应的目标做局部替换。输出完整海报。"]
    for index, item in enumerate(task["replacements"], start=2):
        target = item["target"] or "按素材内容匹配底稿中对应的对象或区域"
        lines.append(f"图片 {index} 是替换原照。目标：{target}。")
    lines.extend([
        "目标为菜品时：以底稿中食物本身为准，锁定中心位置、可见宽高与画面占比。"
        "盘子的尺寸不能代替食物尺寸；禁止将食物缩小、放大或挪位。保留原照的食材、形状、摆盘和质感，"
        "禁止重绘、增减食材或改换餐具。仅允许轻微调整光色、边缘和接触阴影以融入底稿。",
        "目标为场景时：保留原照的主要场景特征，可调整取景、透视与光色来融合。"
        "明确指定「背景」时可替换整张摄影底图，同时保留原海报的文字、排版和装饰。",
    ])
    if any(item["target"].casefold() == "logo" for item in task["replacements"]):
        lines.append("Logo 使用对应的官方原文件。在底稿 Logo 的原中心、原显示框内等比放置，"
                     "保留完整图案、文字、颜色和比例，禁止拉伸、压扁、裁切或重绘。")
    else:
        lines.append("保留底稿上的 Logo 或原有的无 Logo 状态。")
    if task.get("feedback"):
        lines.append(f"本轮反馈：{task['feedback']}。按反馈修正指定问题，仍以图片 1 为大小、位置和排版的基准。")
    return "\n".join(lines)


def _finish_model_batch(db: Any, batch_id: str) -> None:
    tasks = db.background_replace_tasks
    finished = {"$not": {"$elemMatch": {"status": {"$in": original_replacement_runner.ACTIVE}}}}
    filter_base = {"_id": batch_id, "status": "running"}
    time = whole_image.utcnow()
    updated = tasks.update_one(
        {**filter_base, "candidates": {**finished, "$elemMatch": {"status": "succeeded"}}},
        {"$set": {"status": "succeeded", "stage": "done", "progress": 100, "error": None, "updated_at": time}},
    )
    if not updated.matched_count:
        tasks.update_one(
            {**filter_base, "candidates": {"$not": {"$elemMatch": {"status": {"$in": [*original_replacement_runner.ACTIVE, "succeeded"]}}}}},
            {"$set": {"status": "failed", "stage": "failed", "progress": 0, "updated_at": time,
                      "error": {"code": "all_models_failed", "message": "本批没有成功的候选"}}},
        )


def run_model_batch(db: Any, settings: Settings, store: ObjectStore, client: CoreAIClient,
                    batch_id: str, candidate_model: str | None = None) -> None:
    tasks = db.background_replace_tasks
    batch_query = {"_id": batch_id, "kind": "model"}
    if candidate_model is None:
        batch = tasks.find_one_and_update(
            {**batch_query, "status": "queued"},
            {"$set": {"status": "running", "stage": "model_running", "started_at": whole_image.utcnow(),
                      "updated_at": whole_image.utcnow()}}, return_document=ReturnDocument.AFTER)
    else:
        batch = tasks.find_one({**batch_query, "status": "running"})
    if not batch:
        return
    candidates = [(index, item["model"]) for index, item in enumerate(batch["candidates"])
                  if item["status"] == "queued" and (candidate_model is None or candidate_model == item["model"])]
    if not candidates:
        return

    references = []
    images = [{**batch["source"], "role": "替换底图"},
              *({**item, "role": item["target"] or "自动识别"} for item in batch["replacements"])]
    prompt = _model_prompt(batch)
    width, height = batch["source"]["width"], batch["source"]["height"]
    size = gateway_image_size(width, height)

    def run_candidate(index: int, model: str) -> None:
        model_input = {"channel": "gateway", "provider": "core-ai", "endpoint": "/images/edits",
                       "model": model, "prompt": prompt, "images": images,
                       "parameters": {"size": size, "quality": settings.core_ai_image_quality,
                                      "n": 1, "output_format": "png"}}
        candidate = {"model": model, "status": "running", "model_input": model_input,
                     "started_at": whole_image.utcnow()}
        field = f"candidates.{index}"
        claimed = tasks.update_one({"_id": batch_id, f"{field}.status": "queued"}, {"$set": {field: candidate}})
        if not claimed.matched_count:
            return
        try:
            data = client.edit_image(prompt, references, size=size, model=model)
            output = whole_image._save_output_asset(
                db, settings, store, task_id=batch_id, version=1, role=model,
                data=data, target_size=(width, height), allow_letterbox=False)
            candidate.update(status="succeeded", output=output)
        except Exception as exc:  # noqa: BLE001 - the other candidates keep running
            candidate.update(status="failed", error=whole_image._error_text(exc))
        now = whole_image.utcnow()
        candidate["completed_at"] = now
        tasks.update_one({"_id": batch_id}, {"$set": {field: candidate, "updated_at": now}})

    try:
        for item in images:
            references.append(image_ref_from_asset(db, store, item["asset_id"]))
    except Exception as exc:  # noqa: BLE001 - report errors loading shared inputs
        error = whole_image._error_text(exc)
        for index, model in candidates:
            field = f"candidates.{index}"
            tasks.update_one({"_id": batch_id, f"{field}.status": "queued"},
                             {"$set": {field: {"model": model, "status": "failed", "error": error}}})
    else:
        with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
            futures = [pool.submit(run_candidate, index, model) for index, model in candidates]
            for future in futures:
                future.result()
    finally:
        cleanup_image_refs(references)
        _finish_model_batch(db, batch_id)


def list_tasks(db: Any, task_id: str) -> list[dict[str, Any]]:
    _task(db, task_id)
    return list(db.background_replace_tasks.find({"whole_task_id": task_id}).sort("created_at", -1))


def get_task(db: Any, task_id: str, replacement_id: str) -> dict[str, Any]:
    _task(db, task_id)
    replacement = db.background_replace_tasks.find_one({"_id": replacement_id, "whole_task_id": task_id})
    if replacement is None:
        raise StudioError("replacement_missing", "原照替换任务不属于当前海报", 404)
    return replacement


def apply(db: Any, task_id: str, replacement_id: str, *, expected_current_version_id: str,
          expected_current_output_id: str, candidate_model: str | None = None) -> dict[str, Any]:
    current = _task(db, task_id)
    replacement = db.background_replace_tasks.find_one({"_id": replacement_id, "whole_task_id": task_id})
    if replacement is None:
        raise StudioError("replacement_missing", "原照替换任务不属于当前海报", 404)
    candidate = None
    application_key = replacement_id
    if replacement.get("kind") == "model":
        candidate = next((item for item in replacement["candidates"] if item["model"] == candidate_model), None)
        if candidate is None:
            raise StudioError("candidate_required", "请选择本批次的模型候选", 422)
        application_key = f"{replacement_id}_{candidate['model'].replace('.', '_')}"
        output = candidate.get("output") if candidate["status"] == "succeeded" else None
    else:
        output = replacement.get("output") if replacement["status"] == "succeeded" else None

    # A stable ID makes retries idempotent without reselecting an older result.
    version_id = f"wver_original_{application_key}"
    already_applied = _applied_task(db, current, replacement_id, application_key, candidate_model)
    if already_applied is not None:
        return already_applied
    if not output:
        raise StudioError("result_not_ready", "替换结果尚未成功，请先查询并确认结果", 409)
    if not db.assets.find_one({"_id": output["asset_id"]}):
        raise StudioError("result_missing", "替换成图已不存在", 409)
    source = db.whole_versions.find_one({"_id": replacement["source_version_id"], "task_id": task_id})
    if source is None:
        raise StudioError("source_missing", "替换来源版本已不存在", 409)
    token = uuid.uuid4().hex
    prior = db.whole_tasks.find_one_and_update(
        {"_id": task_id, "status": "succeeded", "edit.status": {"$ne": "running"},
         "current_version_id": expected_current_version_id, "current_output_id": expected_current_output_id,
         "candidate_deleted_at": {"$exists": False},
         f"original_replacement_versions.{application_key}": {"$exists": False}},
        {"$set": {"edit.status": "running", "edit.original_apply_token": token}},
        return_document=ReturnDocument.BEFORE)
    if prior is None:
        already_applied = _applied_task(db, _task(db, task_id), replacement_id, application_key, candidate_model)
        if already_applied is not None:
            return already_applied
        raise StudioError("apply_conflict", "当前海报已变化或正在改图，请刷新并重新确认后应用", 409)
    inserted = False
    try:
        # Only the parent document's atomic completion marker proves publication.
        # A version left by an interrupted apply is safe to rebuild while holding this lock.
        db.whole_versions.delete_one({"_id": version_id, "task_id": task_id})
        now = whole_image.utcnow()
        images = [{**replacement["source"], "role": "替换底图"}]
        images.extend({**item, "role": item["target"] or "自动识别"} for item in replacement["replacements"])
        split_all_elements = replacement["input"].get("split_all_elements", False)
        model_input = {"channel": "agent", "endpoint": f"/api/runs/agent/{replacement.get('agent_id')}/trigger",
                       "model": replacement.get("agent_id") or "", "prompt": replacement.get("agent_input") or "",
                       "parameters": {"run_id": replacement.get("run_id"), "split_all_elements": split_all_elements}, "images": images}
        instruction = "原照替换：" + "、".join(item["target"] or "自动识别" for item in replacement["replacements"])
        if split_all_elements:
            instruction += "；全部拆分元素（含艺术字）"
        if candidate:
            model_input = candidate["model_input"]
            if replacement.get("feedback"):
                instruction += f"。本轮反馈：{replacement['feedback']}"
        version = whole_image._version_document(
            task_id=task_id, version=whole_image._next_version(db, task_id),
            parent_version_id=source["_id"], output_asset_id=output["asset_id"], instruction=instruction,
            operation="original_replace", width=output["width"], height=output["height"], model_input=model_input,
            outputs=[{"id": "original_replace_result", "channel": model_input["channel"], "role": "original_replace_result",
                      "label": "原照替换结果", "asset_id": output["asset_id"], "width": output["width"],
                      "height": output["height"], "selectable": True, "model_input": model_input,
                      "artifacts": replacement.get("artifacts") or [], "created_at": now}])
        version.update(_id=version_id, replacement_task_id=replacement_id,
                       source_output_id=replacement["source_output_id"], replacements=replacement["input"]["replacements"])
        db.whole_versions.insert_one(version)
        inserted = True
        committed = db.whole_tasks.update_one(
            {"_id": task_id, "edit.original_apply_token": token},
            {"$set": {"selected_version_id": version_id, "current_version_id": version_id,
                      "current_version": version["version"], "current_output_id": "original_replace_result",
                      "output_asset_id": output["asset_id"], "canvas": {"width": output["width"], "height": output["height"]},
                      "status": "succeeded", "stage": "done", "progress": 100, "error": None,
                      "edit": {"status": "idle"}, "model_input_latest": model_input, "model_input_attempt": None,
                      f"original_replacement_versions.{application_key}": version_id,
                      "provider.status": "completed", "updated_at": now}})
        if committed.modified_count != 1:
            raise StudioError("apply_conflict", "海报状态发生变化，请刷新后重试", 409)
    except Exception:
        if not (_task(db, task_id).get("original_replacement_versions") or {}).get(application_key):
            if inserted:
                db.whole_versions.delete_one({"_id": version_id})
            # The image belongs to the replacement task; rolling back a version must never delete it.
            db.whole_tasks.update_one({"_id": task_id, "edit.original_apply_token": token},
                                     {"$set": {"edit": prior.get("edit") or {"status": "idle"}}})
            raise
    _mark_applied(db, replacement_id, version)
    whole_image._append_message(db, task_id, role="assistant", content=f"已应用原照替换，保存为 v{version['version']}。",
                                version_id=version_id, version=version["version"])
    return _task(db, task_id)


def _mark_applied(db: Any, replacement_id: str, version: dict[str, Any]) -> None:
    tasks = db.background_replace_tasks
    values = {"applied_version_id": version["_id"], "applied_version": version["version"]}
    model_input = version.get("model_input") or {}
    if model_input.get("channel") == "gateway":
        model = model_input["model"]
        task = tasks.find_one({"_id": replacement_id})
        index, candidate = next((i, item) for i, item in enumerate(task["candidates"]) if item["model"] == model)
        tasks.update_one({"_id": replacement_id}, {"$set": {f"candidates.{index}.{key}": value for key, value in values.items()}})
        values.update(applied_candidate_model=model, output=candidate["output"])
    # A retry for an older candidate must not replace the batch's latest download.
    tasks.update_one({"_id": replacement_id, "applied_version": {"$not": {"$gt": version["version"]}}}, {"$set": values})


def _applied_task(db: Any, task: dict[str, Any], replacement_id: str,
                  application_key: str, candidate_model: str | None) -> dict[str, Any] | None:
    applied = task.get("original_replacement_versions") or {}
    version_id = applied.get(application_key) or applied.get(replacement_id)
    if not version_id:
        return None
    version = db.whole_versions.find_one({"_id": version_id, "task_id": task["_id"]})
    if version is None:
        raise StudioError("applied_version_missing", "已应用的替换版本不存在，请检查历史记录", 409)
    # Backfill the candidate for batches already applied under the original batch-wide key.
    _mark_applied(db, replacement_id, version)
    model_input = version.get("model_input") or {}
    if model_input.get("channel") == "gateway" and model_input["model"] != candidate_model:
        return None
    return _task(db, task["_id"])
