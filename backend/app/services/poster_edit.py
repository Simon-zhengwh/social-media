from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.services import agent_image
from app.services.core_ai import CoreAIClient
from app.services.object_store import ObjectStore
from app.time_utils import utc_iso

logger = logging.getLogger(__name__)


class PosterEditError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def validate_asset_ids(db: Any, asset_ids: list[str]) -> list[str]:
    normalized = list(dict.fromkeys(str(item).strip() for item in asset_ids if str(item).strip()))
    if not 1 <= len(normalized) <= 5:
        raise PosterEditError("reference_required", "请选择 1–5 张参考图片")
    for asset_id in normalized:
        asset = db.assets.find_one({"_id": asset_id})
        if asset is None:
            raise PosterEditError("reference_missing", "参考图片不存在", 404)
        mime_type = str(asset.get("mime_type") or "")
        if mime_type and not mime_type.startswith("image/"):
            raise PosterEditError("reference_invalid", "参考文件必须是图片")
    return normalized


def _validate_prompt(prompt: str) -> str:
    text = prompt.strip()
    if not text:
        raise PosterEditError("prompt_required", "请输入修改要求")
    return text


def _task_document(*, batch_id: str, candidate_index: int, candidate_count: int, prompt: str, asset_ids: list[str], created_at: datetime) -> dict[str, Any]:
    return {
        "_id": f"pedit_{uuid.uuid4().hex}",
        "batch_id": batch_id,
        "candidate_index": candidate_index,
        "candidate_count": candidate_count,
        "status": "queued",
        "stage": "queued",
        "progress": 0,
        "input": {"prompt": prompt, "asset_ids": asset_ids},
        "outputs": [],
        "run_id": None,
        "agent_input": None,
        "error": None,
        "created_at": created_at,
        "updated_at": created_at,
    }


def create_task(db: Any, *, prompt: str, asset_ids: list[str]) -> dict[str, Any]:
    normalized = validate_asset_ids(db, asset_ids)
    text = _validate_prompt(prompt)
    batch_id = f"pbat_{uuid.uuid4().hex}"
    task = _task_document(batch_id=batch_id, candidate_index=0, candidate_count=1, prompt=text, asset_ids=normalized, created_at=utcnow())
    db.poster_edit_tasks.insert_one(task)
    return task


def create_batch(db: Any, *, prompt: str, asset_ids: list[str], candidate_count: int) -> tuple[str, list[dict[str, Any]]]:
    count = int(candidate_count)
    if not 1 <= count <= 6:
        raise PosterEditError("candidate_count_invalid", "生成数量必须在 1 到 6 之间")
    normalized = validate_asset_ids(db, asset_ids)
    text = _validate_prompt(prompt)
    batch_id = f"pbat_{uuid.uuid4().hex}"
    now = utcnow()
    tasks = [
        _task_document(batch_id=batch_id, candidate_index=index, candidate_count=count, prompt=text, asset_ids=normalized, created_at=now)
        for index in range(count)
    ]
    db.poster_edit_tasks.insert_many(tasks)
    return batch_id, tasks


def run_batch(db: Any, settings: Settings, store: ObjectStore, client: CoreAIClient, batch_id: str) -> None:
    tasks = list(db.poster_edit_tasks.find({"batch_id": batch_id, "status": "queued"}).sort("candidate_index", 1))
    _execute_tasks(db, settings, store, client, tasks)


def run_task(db: Any, settings: Settings, store: ObjectStore, client: CoreAIClient, task_id: str) -> None:
    task = db.poster_edit_tasks.find_one({"_id": task_id, "status": "queued"})
    _execute_tasks(db, settings, store, client, [task] if task else [])


def resume_tasks(db: Any, settings: Settings, store: ObjectStore, client: CoreAIClient) -> None:
    try:
        tasks = list(
            db.poster_edit_tasks.find(
                {
                    "status": {"$in": ["queued", "running"]},
                    "run_id": {"$nin": [None, ""]},
                    "agent_input": {"$nin": [None, ""]},
                }
            ).sort("candidate_index", 1)
        )
        if not tasks:
            return
        task_ids = [str(task["_id"]) for task in tasks]
        db.poster_edit_tasks.update_many(
            {"_id": {"$in": task_ids}},
            {
                "$set": {
                    "status": "running",
                    "stage": "agent_running",
                    "error": None,
                    "updated_at": utcnow(),
                }
            },
        )
        _run_tasks(db, settings, store, client, tasks)
    except Exception:
        logger.exception("failed to resume poster edit agent runs")


def _execute_tasks(db: Any, settings: Settings, store: ObjectStore, client: CoreAIClient, tasks: list[dict[str, Any]]) -> None:
    if not tasks:
        return
    task_ids = [str(task["_id"]) for task in tasks]
    db.poster_edit_tasks.update_many({"_id": {"$in": task_ids}}, {"$set": {"status": "running", "stage": "preparing", "progress": 10, "updated_at": utcnow()}})
    try:
        input_data = tasks[0].get("input") or {}
        asset_ids = list(input_data.get("asset_ids") or [])
        prompt = str(input_data.get("prompt") or "")
        first_asset = db.assets.find_one({"_id": asset_ids[0]}) if asset_ids else None
        target_width = int(first_asset.get("width") or 0) if first_asset else 0
        target_height = int(first_asset.get("height") or 0) if first_asset else 0
        target_size = (target_width, target_height) if target_width > 0 and target_height > 0 else None
        expires = max(settings.cos_presign_expiry_seconds, settings.core_ai_image_agent_timeout_seconds + 600)
        references: list[tuple[str, str]] = []
        for index, asset_id in enumerate(asset_ids, start=1):
            url = agent_image.asset_public_url(db, settings, store, client, asset_id, expires=expires)
            references.append((f"参考图 {index}", url))
        agent_input = agent_image.build_agent_edit_input(references, prompt)
        db.poster_edit_tasks.update_many({"_id": {"$in": task_ids}}, {"$set": {"stage": "agent_running", "progress": 30, "agent_input": agent_input, "agent_id": settings.core_ai_image_agent_id, "updated_at": utcnow()}})
    except Exception as exc:
        _fail_tasks(db, task_ids, exc)
        return
    _run_tasks(db, settings, store, client, tasks, agent_input)


def _run_tasks(
    db: Any,
    settings: Settings,
    store: ObjectStore,
    client: CoreAIClient,
    tasks: list[dict[str, Any]],
    agent_input: str | None = None,
) -> None:
    with ThreadPoolExecutor(max_workers=min(6, len(tasks))) as executor:
        futures = [
            executor.submit(
                _run_candidate,
                db,
                settings,
                store,
                client,
                task,
                agent_input or str(task.get("agent_input") or ""),
            )
            for task in tasks
        ]
        for future in as_completed(futures):
            future.result()


def _run_candidate(db: Any, settings: Settings, store: ObjectStore, client: CoreAIClient, task: dict[str, Any], agent_input: str) -> None:
    task_id = str(task["_id"])
    try:
        input_data = task.get("input") or {}
        asset_ids = list(input_data.get("asset_ids") or [])
        first_asset = db.assets.find_one({"_id": asset_ids[0]}) if asset_ids else None
        target_width = int(first_asset.get("width") or 0) if first_asset else 0
        target_height = int(first_asset.get("height") or 0) if first_asset else 0
        target_size = (target_width, target_height) if target_width > 0 and target_height > 0 else None
        run_id = str(task.get("run_id") or "") or None

        def remember_run_id(next_run_id: str) -> None:
            db.poster_edit_tasks.update_one(
                {"_id": task_id},
                {
                    "$set": {
                        "status": "running",
                        "stage": "agent_running",
                        "progress": 35,
                        "run_id": next_run_id,
                        "updated_at": utcnow(),
                    }
                },
            )

        result, outputs = agent_image.run_agent_images(
            client,
            agent_id=settings.core_ai_image_agent_id,
            input_text=agent_input,
            timeout_seconds=settings.core_ai_image_agent_timeout_seconds,
            run_id=run_id,
            on_run_id=remember_run_id,
        )
        db.poster_edit_tasks.update_one({"_id": task_id}, {"$set": {"stage": "saving", "progress": 85, "run_id": result.run_id, "agent_output": result.output, "updated_at": utcnow()}})
        saved_outputs: list[dict[str, Any]] = []
        for index, output in enumerate(outputs, start=1):
            saved = agent_image.save_png_asset(db, settings, store, key_prefix=f"poster-edit-{task_id}-{index}", original_stem=f"poster-edit-{task_id}-{index}", data=output.data, role=output.role, target_size=target_size)
            saved_outputs.append({"id": f"pedit_out_{uuid.uuid4().hex}", "role": output.role, "label": output.label, "asset_id": saved["asset_id"], "width": saved["width"], "height": saved["height"], "selectable": output.selectable, "source_url": output.source_url, "model_input": agent_input, "created_at": utcnow()})
        status = "succeeded" if any(item.get("selectable") for item in saved_outputs) else "partial"
        db.poster_edit_tasks.update_one({"_id": task_id}, {"$set": {"status": status, "stage": "done", "progress": 100, "outputs": saved_outputs, "run_id": result.run_id, "updated_at": utcnow()}})
    except Exception as exc:
        _fail_tasks(db, [task_id], exc)


def _fail_tasks(db: Any, task_ids: list[str], exc: Exception) -> None:
    message = getattr(exc, "message", None) or str(exc) or "Agent 修改海报失败"
    code = getattr(exc, "code", "poster_edit_failed")
    db.poster_edit_tasks.update_many({"_id": {"$in": task_ids}}, {"$set": {"status": "failed", "stage": "failed", "progress": 0, "error": {"code": code, "message": str(message)}, "updated_at": utcnow()}})


def task_out(db: Any, doc: dict[str, Any]) -> dict[str, Any]:
    input_data = doc.get("input") or {}
    references = []
    for asset_id in input_data.get("asset_ids") or []:
        asset = db.assets.find_one({"_id": asset_id})
        if asset is None:
            continue
        references.append({"asset_id": str(asset_id), "name": str(asset.get("original_name") or "参考图片"), "preview_url": f"/api/assets/{asset_id}/file", "width": int(asset.get("width") or 0), "height": int(asset.get("height") or 0)})
    task_id = str(doc["_id"])
    outputs = []
    for output in doc.get("outputs") or []:
        output_id = str(output.get("id") or "")
        asset_id = str(output.get("asset_id") or "")
        outputs.append({"id": output_id, "role": output.get("role") or "agent_result", "label": output.get("label") or "修改结果", "asset_id": asset_id, "width": int(output.get("width") or 0), "height": int(output.get("height") or 0), "selectable": bool(output.get("selectable")), "preview_url": f"/api/assets/{asset_id}/file" if asset_id else None, "download_url": f"/api/poster-edits/{task_id}/download?output_id={output_id}" if asset_id and output_id else None, "model_input": output.get("model_input")})
    batch_id = str(doc.get("batch_id") or task_id)
    return {"id": task_id, "batch_id": batch_id, "candidate_index": int(doc.get("candidate_index") or 0), "candidate_count": int(doc.get("candidate_count") or 1), "status": doc.get("status") or "queued", "stage": doc.get("stage") or "queued", "progress": int(doc.get("progress") or 0), "prompt": str(input_data.get("prompt") or ""), "references": references, "outputs": outputs, "agent_id": doc.get("agent_id") or "", "agent_input": doc.get("agent_input"), "run_id": doc.get("run_id"), "error": doc.get("error"), "created_at": utc_iso(doc["created_at"]), "updated_at": utc_iso(doc["updated_at"])}


def batch_out(db: Any, batch_id: str) -> dict[str, Any]:
    tasks = list(db.poster_edit_tasks.find({"batch_id": batch_id}).sort("candidate_index", 1))
    if not tasks:
        raise PosterEditError("batch_missing", "修改批次不存在", 404)
    outputs = [task_out(db, task) for task in tasks]
    return {"id": batch_id, "candidate_count": len(outputs), "tasks": outputs, "created_at": min(item["created_at"] for item in outputs), "updated_at": max(item["updated_at"] for item in outputs)}


def mark_interrupted(db: Any) -> int:
    resumable_query = {
        "status": {"$in": ["queued", "running"]},
        "run_id": {"$nin": [None, ""]},
    }
    resumable = db.poster_edit_tasks.count_documents(resumable_query)
    db.poster_edit_tasks.update_many(
        {
            "status": {"$in": ["queued", "running"]},
            "run_id": {"$in": [None, ""]},
        },
        {
            "$set": {
                "status": "interrupted",
                "stage": "failed",
                "error": {"code": "interrupted", "message": "本地服务重启，修改任务已中断。"},
                "updated_at": utcnow(),
            }
        },
    )
    if resumable:
        db.poster_edit_tasks.update_many(
            resumable_query,
            {
                "$set": {
                    "status": "running",
                    "stage": "agent_running",
                    "error": None,
                    "updated_at": utcnow(),
                }
            },
        )
    return resumable


def download_output(db: Any, store: ObjectStore, settings: Settings, task_id: str, output_id: str):
    task = db.poster_edit_tasks.find_one({"_id": task_id})
    if task is None:
        raise PosterEditError("task_missing", "修改任务不存在", 404)
    output = next((item for item in task.get("outputs") or [] if str(item.get("id")) == output_id), None)
    if output is None:
        raise PosterEditError("output_missing", "结果图片不存在", 404)
    asset = db.assets.find_one({"_id": output.get("asset_id")})
    if asset is None:
        raise PosterEditError("output_missing", "结果图片不存在", 404)
    relative = asset.get("prepared_path") or asset["path"]
    local_path = store.local_path(relative)
    filename = f"poster-edit-{task_id}-{output.get('role') or 'result'}.png"
    if local_path is not None:
        return local_path, filename, None
    url = store.presign(relative, expires=settings.cos_presign_expiry_seconds, download_name=filename)
    return None, filename, url
