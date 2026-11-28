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

from app.api_paths import (asset_url, original_replacement_candidate_source_url, original_replacement_download_url,
                          original_replacement_logo_source_url)
from app.config import Settings
from app.schemas import OriginalReplacementInput
from app.services import agent_image
from app.services import poster_costs
from app.services.canvas import KIE_IMAGE_ASPECT_RATIOS, image_model_supports_canvas
from app.services.core_ai import AgentRunResult, CoreAIClient, CoreAIError
from app.services.object_store import ObjectStore
from app.time_utils import utc_iso

logger = logging.getLogger(__name__)
ACTIVE = ["queued", "running"]
SERIES_LOGO_OUTPUT_RULE = "替换交付的底图和 HTML 画布不要绘制或嵌入 Logo，保存时由应用统一合成官方原文件。"
EDITABLE_HTML_OUTPUT_RULE = (
    "本次为 ORIGINAL_REPLACEMENT（HTML 类）任务，不要走 ELEMENT_REPLACEMENT 或 --element-only 单图路径；"
    "请交付离线可编辑 editor.html 以及与图1同尺寸的不透明最终 PNG（final.png）。"
)
HTML_MISSING_ERROR = "该运行未返回 HTML 编辑器。"
_UNSUPPORTED_FORMAT_MESSAGE = "不支持此格式"

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


def model_candidate(model: str, source: dict[str, Any]) -> dict[str, Any]:
    if not image_model_supports_canvas(model, source["width"], source["height"]):
        return {"model": model, "status": "unsupported", "error": _UNSUPPORTED_FORMAT_MESSAGE}
    return {"model": model, "status": "queued"}


def _model_candidates(doc: dict[str, Any], source: dict[str, Any], candidate_model: str) -> None:
    """One click sends one model request; the remaining models wait as `idle` candidates.

    A model that cannot keep the source ratio is still offered: when it is the requested one
    it runs at its own ratio as a `for_crop` candidate, and the operator crops it back to the
    canvas in the crop dialog instead of being told the model is unusable.
    """
    width, height = source["width"], source["height"]
    requested = candidate_model if candidate_model in KIE_IMAGE_ASPECT_RATIOS else ""
    if not requested:
        requested = next((model for model in KIE_IMAGE_ASPECT_RATIOS
                          if image_model_supports_canvas(model, width, height)), "")
    doc["candidate_model"] = requested
    doc["candidates"] = []
    for model in KIE_IMAGE_ASPECT_RATIOS:
        candidate = model_candidate(model, source)
        if candidate["status"] == "unsupported" and model == requested:
            candidate = {"model": model, "status": "queued", "for_crop": True}
        elif candidate["status"] == "queued" and model != requested:
            candidate["status"] = "idle"
        doc["candidates"].append(candidate)
    if all(candidate["status"] == "unsupported" for candidate in doc["candidates"]):
        doc.update(status="failed", stage="failed", error={
            "code": "unsupported_format", "message": _UNSUPPORTED_FORMAT_MESSAGE,
        })


def create_task(db: Any, *, source_asset_id: str, whole_task_id: str, replacements: list[dict[str, str]],
                split_all_elements: bool = False, metadata: dict[str, Any] | None = None,
                logo_reference_asset_id: str = "", candidate_model: str = "") -> dict[str, Any]:
    from pydantic import ValidationError

    try:
        payload = OriginalReplacementInput(source_asset_id=source_asset_id, replacements=replacements,
                                           split_all_elements=split_all_elements)
    except ValidationError as exc:
        raise OriginalReplacementError("invalid_input", "请选择原图和 1–5 项替换图片", 422) from exc
    source = _asset(db, payload.source_asset_id)
    if source["width"] <= 0 or source["height"] <= 0:
        raise OriginalReplacementError("reference_invalid", "原图尺寸无效，请重新上传", 422)
    items = [{"target": item.target, **_asset(db, item.asset_id)} for item in payload.replacements]
    logo_reference = _asset(db, logo_reference_asset_id) if logo_reference_asset_id else None
    now = utcnow()
    doc = {"_id": f"brep_{uuid.uuid4().hex}", "status": "queued", "stage": "queued", "progress": 0,
           "whole_task_id": whole_task_id, "input": payload.model_dump(), "source": source, "replacements": items,
           "logo_reference": logo_reference,
           "output": None, "agent_id": None, "run_id": None, "agent_input": None, "agent_output": None,
           "started_at": None, "deadline_at": None, "error": None, "created_at": now, "updated_at": now,
           **(metadata or {})}
    if doc.get("kind") == "model":
        _model_candidates(doc, source, candidate_model)
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


def input_images(task: dict[str, Any]) -> list[dict[str, Any]]:
    images = [{**task["source"], "role": "替换底图"},
              *({**item, "role": item["target"] or "自动识别"} for item in task["replacements"])]
    if logo_reference := task.get("logo_reference"):
        images.append({**logo_reference, "role": "官方 Logo 参考"})
    return images


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
        if not item["target"]:
            clause = f"根据图{image_number}的原照内容，自行识别图1中对应的替换对象和位置并替换"
        elif item["target"] == "背景":
            clause = (f"把图1的整张摄影底图（包括照片中的主体和场景）替换成图{image_number}，"
                      "保留图1的文字、Logo、笔刷、贴纸、装饰和原有排版，保持图1的画布尺寸不变")
        clauses.append(clause)
    instruction = "帮我" + "，并且".join(clauses) + "\n" + EDITABLE_HTML_OUTPUT_RULE
    for item in data["replacements"]:
        if item["target"].casefold() == "logo":
            image_number = image_ids.index(item["asset_id"]) + 1
            instruction += (
                f"\n图{image_number}的 Logo 原文件须作为 HTML 中的独立可替换图层，标记 role=logo，"
                "不与文字、笔刷或固定设计层合并；保留原文件的完整图案、颜色与透明度，不重绘或裁切 Logo。"
                "移除固定层中的旧 Logo，保留周围设计和衬底；提供选择 Logo、移动、等比缩放及“更换 Logo”入口。"
                "未要求全部拆层时，只额外拆出 Logo，其他未指定设计保持固定。"
            )
    if data.get("split_all_elements", False):
        logo_layer = "" if task.get("logo_reference") else "、Logo"
        instruction += (
            f"\n同时将整张海报的全部设计元素拆成可独立调整的图层，包括标题、所有文字组{logo_layer}、笔刷、贴纸、插画、装饰、分隔线和照片。"
            "文字按原稿内容与风格制作为独立艺术字透明 PNG 图层，不改写文案；照片使用对应原照，底图完整不透明。"
            "按 skill 的元素拆层流程交付离线 HTML、完整图层，以及与图1同尺寸的不透明最终 PNG。"
        )
    if logo_reference := task.get("logo_reference"):
        image_ids.append(logo_reference["asset_id"])
        instruction += f"\n图{len(image_ids)}是官方 Logo，仅用于识别品牌标识。" + SERIES_LOGO_OUTPUT_RULE
    return instruction + "\n\n" + "\n".join(url(asset_id) for asset_id in image_ids)


# Strong markers never describe a deliverable, even when the label also says
# "预览" or "海报" (e.g. "替换效果预览（新背景 +原设计前景）").
STRONG_AUXILIARY = re.compile(
    r"蒙版|遮罩|对比|校验|报告|原图|参考图|抽图|mask|comparison|overlay|"
    r"manifest|fitted|crop|intermediate|source|input",
    re.I,
)
# Weak markers only veto when the label carries no genuine deliverable word;
# real posters are titled "…全不透明" or "…预览（新背景）".
WEAK_AUXILIARY = re.compile(r"(?<!不)透明|前景|透明层|preview|foreground|layer", re.I)
GENUINE_FINAL = re.compile(r"最终|成图|成品|替换后|修改后|改字后|final|result", re.I)
WEAK_FINAL = re.compile(r"海报|合成|结果|poster", re.I)


def _rate(described: str, *, linked_image: bool) -> int | None:
    """Return a trust score, or None when the label cannot be a deliverable."""
    if STRONG_AUXILIARY.search(described):
        return None
    genuine = bool(GENUINE_FINAL.search(described))
    if WEAK_AUXILIARY.search(described) and not genuine:
        return None
    if genuine:
        return 2
    if WEAK_FINAL.search(described):
        return 1
    return 0 if linked_image else None


def _final_image_candidates(result: AgentRunResult) -> list[str]:
    """Rank every plausible deliverable; the caller settles ties by canvas size."""
    artifacts = {item.download_url: item for item in result.artifacts}
    scored: list[tuple[int, int, str]] = []
    fallback: list[tuple[int, str]] = []
    for order, (image, label, url) in enumerate(
        re.findall(r'(!?)\[([^\]]*)\]\(<?(https://[^\s)>]+)>?(?:\s+"[^"]*")?\)', result.output)
    ):
        artifact = artifacts.get(url)
        if artifact and not artifact.content_type.startswith("image/"):
            continue
        described = label + (f" {artifact.file_name} {artifact.title}" if artifact else "")
        if STRONG_AUXILIARY.search(described):
            continue
        fallback.append((order, url))
        score = _rate(described, linked_image=bool(image))
        if score is not None:
            scored.append((score, order, url))
    if not scored:
        for order, item in enumerate(result.artifacts):
            if not item.content_type.startswith("image/"):
                continue
            described = f"{item.file_name} {item.title}"
            if STRONG_AUXILIARY.search(described):
                continue
            fallback.append((len(scored) + order, item.download_url))
            score = _rate(described, linked_image=False)
            if score:
                scored.append((score, order, item.download_url))
    # Highest trust first, then the Agent's own order.
    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    ranked = list(dict.fromkeys(url for _, _, url in scored))
    if ranked:
        return ranked
    # Every label looked auxiliary (e.g. a run whose only poster is titled
    # "…预览"). The canvas-size check in _save_result still guards the result,
    # so fall back to anything that is not an explicit mask, cutout or comparison.
    return list(dict.fromkeys(url for _, url in fallback))


def _save_editor_artifacts(db: Any, settings: Settings, store: ObjectStore, client: CoreAIClient,
                           task: dict[str, Any], result: AgentRunResult) -> None:
    saved = list(task.get("artifacts") or [])
    failed = False
    html = agent_image.pick_html_artifact(result)
    for artifact in (html, agent_image.pick_bundle_artifact(result)):
        if artifact is None:
            continue
        try:
            existing = db.whole_artifacts.find_one({"task_id": task["_id"], "source_url": artifact.download_url})
            if existing and store.exists(existing["path"]):
                meta = {"id": existing["_id"], "kind": existing["kind"], "file_name": existing["file_name"],
                        "content_type": existing["content_type"], "size": existing["bytes"], "title": existing.get("title", "")}
            else:
                data = client.download_agent_file(artifact.download_url, content_type=artifact.content_type)
                meta = agent_image.save_agent_artifact(db, settings, store, task_id=task["_id"],
                                                       version=0, artifact=artifact, data=data)
            saved = [item for item in saved if item["kind"] != meta["kind"]]
            saved.append(meta)
        except Exception:
            failed = True
            logger.exception("failed to store original replacement artifact for %s", task["_id"])
    artifact_error = (
        "部分编辑器文件未能保存，可重新加载。" if failed
        else HTML_MISSING_ERROR if html is None and not any(item.get("kind") == "html" for item in saved)
        else None
    )
    _update(db, task["_id"], artifacts=saved, artifact_error=artifact_error)


def restore_artifacts(db: Any, settings: Settings, store: ObjectStore, client: CoreAIClient,
                      task_id: str) -> dict[str, Any]:
    task = get_task(db, task_id)
    if task["status"] in ACTIVE or not task.get("run_id"):
        raise OriginalReplacementError("artifacts_not_ready", "请等待运行完成后加载 HTML 编辑器", 409)
    result = client.poll_agent_detail(task["run_id"], include_files=True)
    if result is None:
        raise OriginalReplacementError("artifacts_not_ready", "远端仍在处理中，请稍后加载", 409)
    _save_editor_artifacts(db, settings, store, client, task, result)
    return get_task(db, task_id)


def _save_result(db: Any, settings: Settings, store: ObjectStore, client: CoreAIClient,
                 task: dict[str, Any], result: Any) -> None:
    task_id = task["_id"]
    _update(db, task_id, agent_output=result.output, stage="saving", progress=85)
    _save_editor_artifacts(db, settings, store, client, task, result)
    required = (task["source"]["width"], task["source"]["height"])
    candidates = _final_image_candidates(result)
    if not candidates:
        raise OriginalReplacementError(
            "agent_image_missing",
            "未返回明确的最终成图，请查看 Agent 回复并调整替换项",
            502,
        )
    # The canvas size is the only hard discriminator: same-run candidates
    # include masks, cutouts and comparisons, and the replacement contract
    # already requires the deliverable to match the source exactly.
    data = None
    decoded_sizes: list[tuple[int, int]] = []
    for url in candidates[:8]:
        try:
            candidate = client.download_image(url)
        except CoreAIError:
            continue
        try:
            with Image.open(io.BytesIO(candidate)) as image:
                image.load()
                size = ImageOps.exif_transpose(image).size
        except Exception:
            continue
        decoded_sizes.append(size)
        if size == required:
            data = candidate
            break
    if data is None:
        if decoded_sizes:
            size = decoded_sizes[0]
            raise OriginalReplacementError(
                "output_size_mismatch",
                f"结果尺寸 {size[0]}×{size[1]} 与原图 {required[0]}×{required[1]} 不一致",
                502,
            )
        raise OriginalReplacementError(
            "download_failed",
            "候选成图都无法下载，请稍后重试",
            502,
        )
    saved = agent_image.save_png_asset(
        db,
        settings,
        store,
        key_prefix=f"original-replace-{task_id}",
        original_stem=f"original-replace-{task_id}",
        data=data,
        role="result",
    )
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
            parent = db.whole_tasks.find_one({"_id": task.get("whole_task_id")}) or {}
            with poster_costs.track_task(db, parent, "replace"):
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
                result = client.poll_agent_detail(task["run_id"], include_files=True)
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


def logo_source_png(store: ObjectStore, asset: dict[str, Any]) -> bytes:
    """Official Logo as a lossless same-origin PNG so the browser can composite it."""
    relative = str(asset.get("path") or asset.get("prepared_path") or "")
    if not relative:
        raise OriginalReplacementError("logo_missing", "Logo 原文件不存在", 404)
    try:
        with Image.open(io.BytesIO(store.get_bytes(relative))) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
            image = image.convert("RGBA")
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001 - a broken Logo file must not 500 the workbench
        raise OriginalReplacementError("logo_invalid", "Logo 原文件无法读取", 404) from exc


def _logo_overlay_out(doc: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merchant_id = str(doc.get("merchant_id") or "")
    task_id = str(doc["_id"])
    asset_id = str(overlay.get("asset_id") or "")
    return {
        "asset_id": asset_id,
        "name": overlay.get("name") or "Logo",
        "width": int(overlay.get("width") or 0),
        "height": int(overlay.get("height") or 0),
        "preview_url": asset_url(merchant_id, asset_id),
        "source_url": original_replacement_logo_source_url(
            merchant_id, str(doc.get("whole_task_id") or ""), task_id),
        "box": overlay.get("box"),
    }


def task_out(doc: dict[str, Any]) -> dict[str, Any]:
    output = doc.get("output")
    task_id = str(doc["_id"])
    merchant_id = str(doc.get("merchant_id") or "")
    if output:
        output = {"asset_id": output["asset_id"], "width": output["width"], "height": output["height"],
                  "preview_url": asset_url(merchant_id, str(output["asset_id"])),
                  "download_url": original_replacement_download_url(merchant_id, str(doc["whole_task_id"]), task_id)}
    candidates = []
    overlay = doc.get("logo_overlay") if isinstance(doc.get("logo_overlay"), dict) else None
    for candidate in doc.get("candidates", []):
        item = {key: candidate.get(key) for key in ("model", "status", "error")}
        if candidate.get("review_failed"):
            item["review_failed"] = True
        if candidate.get("for_crop"):
            item["for_crop"] = True
        for key in ("applied_version_id", "applied_version"):
            item[key] = candidate.get(key) or (doc.get(key) if item["model"] == doc.get("applied_candidate_model") else None)
        candidate_output = candidate.get("output")
        if doc["status"] in {"interrupted", "failed"} and item["status"] in ACTIVE:
            item.update(status="failed", error=(doc.get("error") or {}).get("message") or "模型结果未保存")
        if item["status"] == "failed" and not item.get("for_crop"):
            preflight = model_candidate(item["model"], doc["source"])
            if preflight["status"] == "unsupported":
                item.update(preflight)
        # The published preview already carries the official Logo once it was applied.
        shown = candidate.get("composited_output") or candidate_output
        item["output"] = {
            "width": shown["width"], "height": shown["height"],
            "preview_url": asset_url(merchant_id, shown["asset_id"]),
        } if shown else None
        if candidate.get("logo_box"):
            item["logo_box"] = candidate["logo_box"]
        if overlay is not None and candidate_output:
            # Clean base for re-opening "调整 Logo": never composites on top of a Logo twice.
            item["base_source_url"] = original_replacement_candidate_source_url(
                merchant_id, str(doc["whole_task_id"]), task_id, item["model"])
        candidates.append(item)
    return {"id": task_id, "kind": doc.get("kind", "agent"), "candidates": candidates,
            **{key: doc.get(key) for key in ["status", "stage", "progress", "source", "replacements", "logo_reference", "feedback",
            "retry_of", "applied_candidate_model", "candidate_model",
            "agent_id", "run_id", "agent_input", "agent_output", "error", "whole_task_id", "source_version_id",
            "source_output_id", "source_version", "applied_version_id", "applied_version"]}, "output": output,
            "logo_overlay": _logo_overlay_out(doc, overlay) if overlay and overlay.get("box") else None,
            "split_all_elements": (doc.get("input") or {}).get("split_all_elements", False),
            "artifacts": agent_image.artifacts_out(doc, str(doc.get("merchant_id") or "")),
            "artifact_error": doc.get("artifact_error"),
            **{key: utc_iso(doc.get(key)) for key in ["created_at", "updated_at", "started_at", "deadline_at"]}}
