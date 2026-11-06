from __future__ import annotations

import uuid
from typing import Any

from pymongo import ReturnDocument

from app.services import original_replacement_runner, whole_image
from app.services.whole_image import StudioError


def _task(db: Any, task_id: str) -> dict[str, Any]:
    task = db.whole_tasks.find_one({"_id": task_id})
    if task is None:
        raise StudioError("task_missing", "海报任务不存在", 404)
    return task


def create(db: Any, task_id: str, *, source_version_id: str, source_output_id: str,
           replacements: list[dict[str, str]]) -> dict[str, Any]:
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
    task = original_replacement_runner.create_task(db, source_asset_id=output["asset_id"], whole_task_id=task_id, replacements=replacements)
    association = {"whole_task_id": task_id, "source_version_id": source_version_id,
                   "source_output_id": source_output_id, "source_version": version["version"]}
    db.background_replace_tasks.update_one({"_id": task["_id"]}, {"$set": association})
    return {**task, **association}


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
          expected_current_output_id: str) -> dict[str, Any]:
    current = _task(db, task_id)
    replacement = db.background_replace_tasks.find_one({"_id": replacement_id, "whole_task_id": task_id})
    if replacement is None:
        raise StudioError("replacement_missing", "原照替换任务不属于当前海报", 404)
    # A stable ID makes retries idempotent without reselecting an older result.
    version_id = f"wver_original_{replacement_id}"
    already_applied = _applied_task(db, current, replacement_id)
    if already_applied is not None:
        return already_applied
    output = replacement.get("output")
    if replacement["status"] != "succeeded" or not output:
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
         f"original_replacement_versions.{replacement_id}": {"$exists": False}},
        {"$set": {"edit.status": "running", "edit.original_apply_token": token}},
        return_document=ReturnDocument.BEFORE)
    if prior is None:
        already_applied = _applied_task(db, _task(db, task_id), replacement_id)
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
        images.extend({**item, "role": item["target"]} for item in replacement["replacements"])
        model_input = {"channel": "agent", "endpoint": f"/api/runs/agent/{replacement.get('agent_id')}/trigger",
                       "model": replacement.get("agent_id") or "", "prompt": replacement.get("agent_input") or "",
                       "parameters": {"run_id": replacement.get("run_id")}, "images": images}
        instruction = "原照替换：" + "、".join(item["target"] for item in replacement["replacements"])
        version = whole_image._version_document(
            task_id=task_id, version=whole_image._next_version(db, task_id),
            parent_version_id=source["_id"], output_asset_id=output["asset_id"], instruction=instruction,
            operation="original_replace", width=output["width"], height=output["height"], model_input=model_input,
            outputs=[{"id": "original_replace_result", "channel": "agent", "role": "original_replace_result",
                      "label": "原照替换结果", "asset_id": output["asset_id"], "width": output["width"],
                      "height": output["height"], "selectable": True, "model_input": model_input, "created_at": now}])
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
                      f"original_replacement_versions.{replacement_id}": version_id,
                      "provider.status": "completed", "updated_at": now}})
        if committed.modified_count != 1:
            raise StudioError("apply_conflict", "海报状态发生变化，请刷新后重试", 409)
    except Exception:
        if not (_task(db, task_id).get("original_replacement_versions") or {}).get(replacement_id):
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
    db.background_replace_tasks.update_one({"_id": replacement_id}, {"$set": {
        "applied_version_id": version["_id"], "applied_version": version["version"]}})


def _applied_task(db: Any, task: dict[str, Any], replacement_id: str) -> dict[str, Any] | None:
    version_id = (task.get("original_replacement_versions") or {}).get(replacement_id)
    if not version_id:
        return None
    version = db.whole_versions.find_one({"_id": version_id, "task_id": task["_id"]})
    if version is None:
        raise StudioError("applied_version_missing", "已应用的替换版本不存在，请检查历史记录", 409)
    _mark_applied(db, replacement_id, version)
    return _task(db, task["_id"])
