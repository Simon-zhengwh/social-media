from __future__ import annotations

import io
import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from PIL import Image, ImageOps
from pymongo import ReturnDocument

from app.api_paths import asset_url, original_replacement_download_url
from app.config import Settings
from app.schemas import OriginalReplacementInput
from app.services import agent_image
from app.services.core_ai import AgentRunResult, CoreAIClient, CoreAIError
from app.services.object_store import ObjectStore
from app.time_utils import utc_iso

logger = logging.getLogger(__name__)
ACTIVE = ["queued", "running"]


class OriginalReplacementError(CoreAIError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _asset(db: Any, asset_id: str) -> dict[str, Any]:
    asset = db.assets.find_one({"_id": asset_id})
    if asset is None:
        raise OriginalReplacementError("reference_missing", "所选图片不存在，请重新选择", 404)
    if not str(asset.get("mime_type") or "").startswith("image/"):
        raise OriginalReplacementError("reference_invalid", "原图和替换素材必须是图片", 422)
    return {"asset_id": asset_id, "name": asset.get("original_name") or "图片",
            "preview_url": asset_url(str(asset.get("merchant_id") or ""), asset_id),
            "width": int(asset.get("width") or 0), "height": int(asset.get("height") or 0)}


def create_task(db: Any, *, source_asset_id: str, whole_task_id: str, replacements: list[dict[str, str]]) -> dict[str, Any]:
    from pydantic import ValidationError

    try:
        payload = OriginalReplacementInput(source_asset_id=source_asset_id, replacements=replacements)
    except ValidationError as exc:
        raise OriginalReplacementError("invalid_input", "请选择原图并填写 1–5 项替换元素和对应图片", 422) from exc
    source = _asset(db, payload.source_asset_id)
    if source["width"] <= 0 or source["height"] <= 0:
        raise OriginalReplacementError("reference_invalid", "原图尺寸无效，请重新上传", 422)
    items = [{"target": item.target, **_asset(db, item.asset_id)} for item in payload.replacements]
    now = utcnow()
    doc = {"_id": f"brep_{uuid.uuid4().hex}", "status": "queued", "stage": "queued", "progress": 0,
           "whole_task_id": whole_task_id, "input": payload.model_dump(), "source": source, "replacements": items,
           "output": None, "agent_id": None, "run_id": None, "agent_input": None, "agent_output": None,
           "started_at": None, "deadline_at": None, "error": None, "created_at": now, "updated_at": now}
    # Keep the existing collection so linked replacement history survives retirement.
    db.background_replace_tasks.insert_one(doc)
    return db.background_replace_tasks.find_one({"_id": doc["_id"]})


def _update(db: Any, task_id: str, **values: Any) -> None:
    db.background_replace_tasks.update_one({"_id": task_id}, {"$set": {**values, "updated_at": utcnow()}})


def _fail(db: Any, task_id: str, exc: Exception) -> None:
    code = getattr(exc, "code", "original_replace_failed")
    status = "partial" if code in {"agent_image_missing", "agent_image_ambiguous"} else "failed"
    _update(db, task_id, status=status, stage="failed", progress=0,
            error={"code": code, "message": getattr(exc, "message", None) or str(exc) or "替换失败"})


def _build_input(db: Any, settings: Settings, store: ObjectStore, client: CoreAIClient, task: dict[str, Any]) -> str:
    expires = max(settings.cos_presign_expiry_seconds, settings.core_ai_background_replace_agent_timeout_seconds + 600)
    urls: dict[str, str] = {}

    def url(asset_id: str) -> str:
        if asset_id not in urls:
            urls[asset_id] = agent_image.asset_public_url(db, settings, store, client, asset_id, expires=expires, use_original=True)
        return urls[asset_id]

    data = task["input"]
    source = dict(task["source"])
    source_asset = db.assets.find_one({"_id": data["source_asset_id"]})
    if not source_asset or not source_asset.get("path"):
        raise OriginalReplacementError("reference_missing", "原图文件不存在，请重新上传", 422)
    # Asset metadata describes the prepared thumbnail; replacement must use the full original canvas.
    with Image.open(io.BytesIO(store.get_bytes(source_asset["path"]))) as image:
        width, height = image.size
        if image.getexif().get(274) in {5, 6, 7, 8}:
            width, height = height, width
    source.update(width=width, height=height)
    task["source"] = source
    _update(db, task["_id"], source=source)
    image_ids = list(dict.fromkeys([data["source_asset_id"], *[item["asset_id"] for item in data["replacements"]]]))
    clauses = []
    for item in data["replacements"]:
        image_number = image_ids.index(item["asset_id"]) + 1
        clause = f"把图1的{item['target']}替换成图{image_number}"
        if item["target"] == "背景":
            clause = (f"把图1的整张摄影底图（包括照片中的主体和场景）替换成图{image_number}，"
                      "保留图1的文字、Logo、笔刷、贴纸、装饰和原有排版，保持图1的画布尺寸不变")
        clauses.append(clause)
    instruction = "帮我" + "，并且".join(clauses)
    return instruction + "\n\n" + "\n".join(url(asset_id) for asset_id in image_ids)


def _final_image_url(result: AgentRunResult) -> str:
    auxiliary = re.compile(r"蒙版|遮罩|对比|预览|校验|报告|前景|透明层|原图|参考图|mask|comparison|preview|overlay|manifest|foreground|layer|fitted|crop|intermediate|source|input", re.I)
    final = re.compile(r"最终|成图|成品|替换结果|修改结果|final|result", re.I)
    links = re.findall(r'(!?)\[([^\]]*)\]\(<?(https://[^\s)>]+)>?(?:\s+"[^"]*")?\)', result.output)
    artifacts = {item.download_url: item for item in result.artifacts}
    finals, images = [], []
    for image, label, url in links:
        artifact = artifacts.get(url)
        description = label + (f" {artifact.file_name} {artifact.title}" if artifact else "")
        if auxiliary.search(description):
            continue
        if final.search(description):
            finals.append(url)
        elif image:
            images.append(url)
    # The final reply identifies deliverables; artifacts may also contain intermediate masks and previews.
    candidates = finals or images
    if not candidates:
        candidates = [item.download_url for item in result.artifacts
                      if final.search(f"{item.file_name} {item.title}")
                      and not auxiliary.search(f"{item.file_name} {item.title}")]
    urls = list(dict.fromkeys(candidates))
    if not urls:
        raise OriginalReplacementError("agent_image_missing", "未返回明确的最终成图，请查看 Agent 回复并调整替换项", 502)
    if len(urls) != 1:
        raise OriginalReplacementError("agent_image_ambiguous", "Agent 返回多个可能的最终成图，无法确定替换结果，请查看回复", 502)
    return urls[0]


def _save_result(db: Any, settings: Settings, store: ObjectStore, client: CoreAIClient,
                 task: dict[str, Any], result: Any) -> None:
    task_id = task["_id"]
    _update(db, task_id, agent_output=result.output, stage="saving", progress=85)
    data = client.download_image(_final_image_url(result))
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            size = ImageOps.exif_transpose(image).size
    except Exception as exc:
        raise OriginalReplacementError("invalid_output", "最终图片无法解码", 502) from exc
    required = (task["source"]["width"], task["source"]["height"])
    if size != required:
        raise OriginalReplacementError("output_size_mismatch", f"结果尺寸 {size[0]}×{size[1]} 与原图 {required[0]}×{required[1]} 不一致", 502)
    saved = agent_image.save_png_asset(db, settings, store, key_prefix=f"original-replace-{task_id}",
                                      original_stem=f"original-replace-{task_id}", data=data, role="result")
    _update(db, task_id, output=saved, status="succeeded", stage="done", progress=100, error=None)


def run_task(db: Any, settings: Settings, store: ObjectStore, client: CoreAIClient, task_id: str,
             *, refresh: bool = False) -> None:
    # Claim before work so duplicate background callbacks cannot trigger the Agent twice.
    task = db.background_replace_tasks.find_one_and_update(
        {"_id": task_id, "status": "queued"},
        {"$set": {"status": "running", "stage": "preparing", "progress": 10, "updated_at": utcnow()}},
        return_document=ReturnDocument.AFTER)
    if task is None:
        return
    try:
        if refresh and not task.get("run_id"):
            raise OriginalReplacementError("run_missing", "没有已知运行记录，无法查询已有结果", 409)
        if not task.get("run_id"):
            text = _build_input(db, settings, store, client, task)
            agent_id = settings.core_ai_background_replace_agent_id
            timeout = settings.core_ai_background_replace_agent_timeout_seconds
            _update(db, task_id, agent_input=text, agent_id=agent_id)
            run_id = client.trigger_agent(agent_id, text, timeout_seconds=timeout)
            started = utcnow()
            _update(db, task_id, run_id=run_id, started_at=started,
                    deadline_at=started + timedelta(seconds=timeout), stage="agent_running", progress=30)
            task = db.background_replace_tasks.find_one({"_id": task_id})
        else:
            _update(db, task_id, stage="agent_running", progress=30)
        deadline = _utc(task["deadline_at"]) if task.get("deadline_at") else utcnow()
        while True:
            try:
                result = client.poll_agent_detail(task["run_id"])
            except (httpx.TimeoutException, httpx.NetworkError):
                if refresh:
                    raise OriginalReplacementError("agent_unavailable", "查询网络异常，可稍后查询已有结果", 502)
                result = None
            if result is not None:
                _save_result(db, settings, store, client, task, result)
                return
            remaining = (deadline - utcnow()).total_seconds()
            if remaining <= 0:
                raise OriginalReplacementError("agent_timeout", "已达到本地等待上限，远端可能仍在处理，可稍后查询已有结果", 504)
            time.sleep(min(5, remaining))
    except Exception as exc:
        logger.warning("original replacement %s failed: %s", task_id, exc)
        _fail(db, task_id, exc)


def queue_refresh(db: Any, task_id: str) -> dict[str, Any]:
    task = get_task(db, task_id)
    if not task.get("run_id"):
        raise OriginalReplacementError("run_missing", "任务没有已知运行记录，请检查错误后重新提交", 409)
    if task["status"] in ACTIVE or task["status"] == "succeeded":
        return task
    db.background_replace_tasks.update_one(
        {"_id": task_id, "status": {"$nin": [*ACTIVE, "succeeded"]}},
        {"$set": {"status": "queued", "stage": "queued", "error": None, "updated_at": utcnow()}})
    return get_task(db, task_id)


def mark_interrupted(db: Any) -> int:
    db.background_replace_tasks.update_many(
        {"whole_task_id": {"$type": "string", "$ne": ""}, "status": {"$in": ACTIVE}, "run_id": {"$in": [None, ""]}},
        {"$set": {"status": "interrupted", "stage": "failed", "updated_at": utcnow(),
                  "error": {"code": "interrupted", "message": "服务重启，任务尚未保存运行记录；请检查后重新提交"}}})
    return db.background_replace_tasks.count_documents({"whole_task_id": {"$type": "string", "$ne": ""}, "status": {"$in": ACTIVE}})


def resume_tasks(db: Any, settings: Settings, store: ObjectStore, client: CoreAIClient) -> None:
    tasks = list(db.background_replace_tasks.find({"whole_task_id": {"$type": "string", "$ne": ""}, "status": {"$in": ACTIVE}, "run_id": {"$nin": [None, ""]}}))
    if not tasks:
        return
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = []
        for task in tasks:
            db.background_replace_tasks.update_one({"_id": task["_id"]}, {"$set": {"status": "queued"}})
            futures.append(executor.submit(run_task, db, settings, store, client, task["_id"]))
        for future in futures:
            future.result()


def get_task(db: Any, task_id: str) -> dict[str, Any]:
    doc = db.background_replace_tasks.find_one({"_id": task_id})
    if doc is None:
        raise OriginalReplacementError("task_missing", "替换任务不存在", 404)
    return doc


def task_out(doc: dict[str, Any]) -> dict[str, Any]:
    output = doc.get("output")
    task_id = str(doc["_id"])
    if output:
        merchant_id = str(doc.get("merchant_id") or "")
        output = {"asset_id": output["asset_id"], "width": output["width"], "height": output["height"],
                  "preview_url": asset_url(merchant_id, str(output["asset_id"])),
                  "download_url": original_replacement_download_url(merchant_id, str(doc["whole_task_id"]), task_id)}
    return {"id": task_id, **{key: doc.get(key) for key in ["status", "stage", "progress", "source", "replacements",
            "agent_id", "run_id", "agent_input", "agent_output", "error", "whole_task_id", "source_version_id",
            "source_output_id", "source_version", "applied_version_id", "applied_version"]}, "output": output,
            **{key: utc_iso(doc.get(key)) for key in ["created_at", "updated_at", "started_at", "deadline_at"]}}
