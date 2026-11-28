from __future__ import annotations

import base64
import binascii
import hashlib
import io
import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from PIL import Image, ImageOps
from pymongo import ReturnDocument

from app.config import Settings
from app.schemas import gateway_model_name
from app.services import original_replacement_runner, whole_image
from app.services.canvas import KIE_IMAGE_ASPECT_RATIOS, gateway_image_size, image_model_supports_canvas
from app.services import poster_costs
from app.services.core_ai import CoreAIClient
from app.services.image_ref import cleanup_image_refs, image_ref_from_asset
from app.services.object_store import ObjectStore
from app.services.whole_image import StudioError

logger = logging.getLogger(__name__)

# A click on "仅替换元素" spends one model request. The other models stay as idle candidates
# until an operator asks for them, so a three-way comparison costs three clicks, not three calls.

# A series page's Logo is always the group's official file, composited by the series layer.
# Asking the replacement Agent to redraw or swap the Logo makes the result show it twice.
_LOGO_TARGET = re.compile(r"logo|标识|标志|商标", re.IGNORECASE)


def is_series_task(task: dict[str, Any]) -> bool:
    return isinstance(task.get("series"), dict)


def series_logo_asset_ids(task: dict[str, Any]) -> set[str]:
    """Official Logo assets bound to this page by the series group or the brand."""
    series = task.get("series") or {}
    ids = {
        str(series.get("logo_asset_id") or ""),
        str((task.get("input") or {}).get("logo_asset_id") or ""),
    }
    ids.update(str(value) for value in (task.get("brand") or {}).get("logo_asset_ids") or [])
    ids.discard("")
    return ids


def replacement_requests_logo(task: dict[str, Any], replacements: list[Any] | None, *,
                              official_logo_id: str = "") -> bool:
    """True when a replacement record explicitly asks to replace the Logo itself."""
    logo_ids = series_logo_asset_ids(task)
    for item in replacements or []:
        if not isinstance(item, dict):
            continue
        target = str(item.get("target") or "").strip()
        asset_id = str(item.get("asset_id") or "").strip()
        if target and _LOGO_TARGET.search(target):
            return True
        if asset_id and (asset_id == official_logo_id or asset_id in logo_ids):
            return True
    return False


def _reject_series_logo_replacement(task: dict[str, Any], replacements: list[Any] | None, *,
                                    official_logo_id: str = "") -> None:
    if is_series_task(task) and replacement_requests_logo(task, replacements, official_logo_id=official_logo_id):
        raise StudioError(
            "series_logo_replacement_not_allowed",
            "系列图页面的 Logo 由后台统一叠加，原照替换只能替换照片。请去掉 Logo 后重新生成仅替换照片的原照替换。",
            422,
        )


# ---------------------------------------------------------------------------
# Logo overlay: the edit model erases the old Logo, the workbench composites the
# official file on the client canvas. Keep the split so the backend never has to
# splice pixels and can move to Java later.
# ---------------------------------------------------------------------------

_LOGO_MIN_RATIO = 0.015
_LOGO_MAX_AREA = 0.35
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_LOGO_DETECT_PROMPT = (
    "找出这张海报底稿中原来印着的品牌 Logo 或商标徽章，连同紧贴它的底衬。"
    "品牌 Logo 通常只占画面宽度的 5%–25%，不要把它和海报主标题、副标题或正文艺术字混淆。"
    "严格只返回一个 JSON 对象：找到时返回 "
    '{"found": true, "x": 0.05, "y": 0.04, "width": 0.18, "height": 0.12}，'
    "其中 x、y、width、height 都是相对整张海报宽高的 0.0–1.0 归一化比例；"
    '画面中没有任何品牌 Logo 时返回 {"found": false}。'
)


def _logo_item(batch: dict[str, Any]) -> dict[str, Any] | None:
    """The replacement row whose target is the brand Logo, for single model batches."""
    if batch.get("kind") != "model" or batch.get("logo_reference"):
        return None
    return next((item for item in batch.get("replacements") or []
                 if _LOGO_TARGET.search(str(item.get("target") or ""))), None)


def _photo_replacements(batch: dict[str, Any]) -> list[dict[str, Any]]:
    """Replacement rows that stay photo swaps; the Logo is composited by the workbench."""
    logo = _logo_item(batch)
    return [item for item in batch.get("replacements") or [] if item is not logo]


def _model_input_images(batch: dict[str, Any]) -> list[dict[str, Any]]:
    """Image 1 is the base poster; the official Logo never reaches the edit model."""
    images = [{**batch["source"], "role": "替换底图"},
              *({**item, "role": item["target"] or "自动识别"} for item in _photo_replacements(batch))]
    if logo_reference := batch.get("logo_reference"):
        images.append({**logo_reference, "role": "官方 Logo 参考"})
    return images


def _original_image_size(store: ObjectStore, asset: dict[str, Any]) -> tuple[int, int]:
    """Real pixel size of an asset file; metadata may only describe a thumbnail."""
    relative = str(asset.get("path") or asset.get("prepared_path") or "")
    if relative:
        try:
            with Image.open(io.BytesIO(store.get_bytes(relative))) as image:
                return ImageOps.exif_transpose(image).size
        except Exception:  # noqa: BLE001 - fall back to the stored metadata
            logger.warning("could not read the size of %s", relative)
    return max(1, int(asset.get("width") or 1)), max(1, int(asset.get("height") or 1))


def _poster_data_url(store: ObjectStore, asset: dict[str, Any]) -> str:
    relative = str(asset.get("path") or asset.get("prepared_path") or "")
    if not relative:
        raise original_replacement_runner.OriginalReplacementError("source_missing", "底稿文件不存在", 422)
    return _review_image_url(store.get_bytes(relative))


def _review_image_url(data: bytes) -> str:
    with Image.open(io.BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source)
        image.load()
        image = image.convert("RGB")
    image.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _review_model_candidate(client: CoreAIClient, settings: Settings, source_url: str,
                            data: bytes, *, remove_logo: bool) -> str | None:
    violations = {"text_changed": "候选未保留底稿文案", "logo_remaining": "候选底图仍有 Logo"}
    logo_rule = (
        "图 2 是叠加官方 Logo 前的底图。检查整张图，特别是图 1 原有品牌标识附近。"
        "如仍有品牌图形、独立品牌字标或残留笔画，或画出了新的品牌标识，记为 logo_remaining。"
        "排除品牌标识，继续核对海报文案。"
        if remove_logo else "只审核海报文案。"
    )
    prompt = (
        "图 1 是海报底稿，图 2 是替换摄影素材后的候选图。逐字对照标题、副标题和其他设计文案。"
        "如有换字、错字、删字、加字或整句重写，记为 text_changed。"
        "忽略断行、空格、纯字体变化，以及被替换照片本身的文字。"
        f"{logo_rule}"
        '仅返回 JSON。合格时返回 {"status":"pass","violations":[]}。'
        '不合格时返回 {"status":"fail","violations":[...]}，列出所有适用的问题代码。'
    )
    content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": source_url}},
        {"type": "image_url", "image_url": {"url": _review_image_url(data)}},
    ]
    try:
        result = client.chat_json(
            [{"role": "user", "content": content}], model=settings.asset_index_model_name,
            timeout_seconds=min(settings.asset_index_timeout_seconds, 45),
        )
    except Exception as exc:
        logger.warning("Replacement candidate review unavailable: %s", type(exc).__name__)
        return "暂时无法校验替换结果，请重试"
    issues = result.get("violations")
    if isinstance(issues, list):
        if result.get("status") == "pass" and not issues:
            return None
        if result.get("status") == "fail" and issues and all(
            isinstance(issue, str) and issue in violations for issue in issues
        ) and (remove_logo or "logo_remaining" not in issues):
            if set(issues) == violations.keys():
                return "候选的文案和 Logo 均未通过校验"
            return violations[issues[0]]
    return "暂时无法校验替换结果，请重试"


def _normalize_logo_box(payload: Any, source_w: int, source_h: int) -> dict[str, int] | None:
    """Turn the model's normalized answer into a pixel box, or None when it is unusable."""
    if not isinstance(payload, dict) or payload.get("found") is not True:
        return None
    try:
        x, y, width, height = (float(payload[key]) for key in ("x", "y", "width", "height"))
    except (KeyError, TypeError, ValueError):
        return None
    values = (x, y, width, height)
    if max(values) > 1.0:
        # Answers occasionally arrive in per-mille or in pixels; rescale defensively.
        divisor = 1000.0 if max(values) <= 1000.0 else float(max(source_w, source_h, 1))
        x, y, width, height = (value / divisor for value in values)
    x, y = min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)
    width, height = min(width, 1.0 - x), min(height, 1.0 - y)
    # A sliver is noise and a huge box is the poster headline, not a Logo.
    if width <= _LOGO_MIN_RATIO or height <= _LOGO_MIN_RATIO or width * height > _LOGO_MAX_AREA:
        return None
    return {"x": round(x * source_w), "y": round(y * source_h),
            "width": max(1, round(width * source_w)), "height": max(1, round(height * source_h))}


def _fit_logo_box(erase_box: dict[str, int], logo_w: int, logo_h: int,
                  source_w: int, source_h: int) -> dict[str, Any]:
    """Equal-ratio box matching the old Logo's visual size, centred on its frame."""
    scale = min(erase_box["width"] / max(logo_w, 1), erase_box["height"] / max(logo_h, 1))
    width = max(1, min(source_w, round(logo_w * scale)))
    height = max(1, min(source_h, round(logo_h * scale)))
    center_x = erase_box["x"] + erase_box["width"] / 2
    center_y = erase_box["y"] + erase_box["height"] / 2
    return {"x": max(0, min(source_w - width, round(center_x - width / 2))),
            "y": max(0, min(source_h - height, round(center_y - height / 2))),
            "width": width, "height": height, "detected": True}


def _logo_anchor(target: str) -> tuple[str, str]:
    text = str(target or "")
    vertical = "bottom" if "下" in text else "top"
    if "右" in text:
        return "right", vertical
    if "左" in text:
        return "left", vertical
    if "中" in text:
        return "center", vertical
    return "left", "top"


def _fallback_logo_box(logo_item: dict[str, Any], logo_w: int, logo_h: int,
                       source_w: int, source_h: int) -> dict[str, Any]:
    """Deterministic corner box used when the poster's old Logo cannot be located."""
    horizontal, vertical = _logo_anchor(logo_item.get("target") or "")
    margin = max(12, round(min(source_w, source_h) * 0.045))
    scale = (min(source_w, source_h) * 0.20) / max(logo_w, logo_h)
    width = max(1, min(source_w, round(logo_w * scale)))
    height = max(1, min(source_h, round(logo_h * scale)))
    x = {"left": margin, "right": source_w - margin - width,
         "center": round((source_w - width) / 2)}[horizontal]
    y = margin if vertical == "top" else source_h - margin - height
    return {"x": max(0, min(source_w - width, x)), "y": max(0, min(source_h - height, y)),
            "width": width, "height": height, "detected": False}


def _detect_logo_box(db: Any, settings: Settings, store: ObjectStore, client: CoreAIClient,
                     batch: dict[str, Any], parent: dict[str, Any]) -> dict[str, int] | None:
    asset = db.assets.find_one({"_id": batch["source"]["asset_id"]}) or {}
    messages = [{"role": "user", "content": [
        {"type": "text", "text": _LOGO_DETECT_PROMPT},
        {"type": "image_url", "image_url": {"url": _poster_data_url(store, asset)}},
    ]}]
    # The default design model cannot read images, so the asset index model is explicit here.
    with poster_costs.track_task(db, parent, "replace"):
        payload = client.chat_json(
            messages,
            model=settings.asset_index_model,
            timeout_seconds=min(settings.asset_index_timeout_seconds, 45),
        )
    return _normalize_logo_box(payload, int(batch["source"]["width"]), int(batch["source"]["height"]))


def _logo_overlay(db: Any, settings: Settings, store: ObjectStore, client: CoreAIClient,
                  batch: dict[str, Any], parent: dict[str, Any]) -> dict[str, Any] | None:
    """Locate the batch's Logo once and persist it, so every candidate shares one plan."""
    logo_item = _logo_item(batch)
    if logo_item is None:
        return None
    stored = batch.get("logo_overlay")
    if isinstance(stored, dict) and stored.get("box"):
        return stored
    source_w = int(batch["source"]["width"])
    source_h = int(batch["source"]["height"])
    logo_asset = db.assets.find_one({"_id": logo_item["asset_id"]}) or {}
    logo_w, logo_h = _original_image_size(store, logo_asset)
    try:
        erase_box = _detect_logo_box(db, settings, store, client, batch, parent)
    except Exception as exc:  # noqa: BLE001 - locating the old Logo must never fail the batch
        logger.warning("logo detection failed for %s: %s", batch.get("_id"), exc)
        erase_box = None
    box = _fit_logo_box(erase_box, logo_w, logo_h, source_w, source_h) if erase_box else None
    overlay = {
        "asset_id": logo_item["asset_id"],
        "name": logo_item.get("name") or "Logo",
        "width": logo_w,
        "height": logo_h,
        "box": box or _fallback_logo_box(logo_item, logo_w, logo_h, source_w, source_h),
        "erase_box": erase_box,
    }
    db.background_replace_tasks.update_one(
        {"_id": batch["_id"], "logo_overlay": {"$exists": False}}, {"$set": {"logo_overlay": overlay}})
    # Another candidate may have stored the plan first; reuse that one.
    winner = (db.background_replace_tasks.find_one({"_id": batch["_id"]}, {"logo_overlay": 1}) or {}).get("logo_overlay")
    return winner if isinstance(winner, dict) and winner.get("box") else overlay


def _decode_composited_png(payload: str, size: tuple[int, int]) -> bytes:
    """Validate the client-composited PNG; anything else must not become a version."""
    text = str(payload or "").strip()
    if text.startswith("data:"):
        text = text.partition(",")[2]
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise StudioError("invalid_composited_png", "合成图片格式或尺寸无效", 422) from exc
    if not data.startswith(_PNG_MAGIC):
        raise StudioError("invalid_composited_png", "合成图片格式或尺寸无效", 422)
    try:
        with Image.open(io.BytesIO(data)) as image:
            decoded = ImageOps.exif_transpose(image).size
    except Exception as exc:  # noqa: BLE001 - untrusted client payload
        raise StudioError("invalid_composited_png", "合成图片格式或尺寸无效", 422) from exc
    if decoded != size:
        raise StudioError("invalid_composited_png", "合成图片格式或尺寸无效", 422)
    return data



def _task(db: Any, task_id: str) -> dict[str, Any]:
    task = db.whole_tasks.find_one({"_id": task_id})
    if task is None:
        raise StudioError("task_missing", "海报任务不存在", 404)
    return task


def create(db: Any, task_id: str, *, source_version_id: str, source_output_id: str,
           replacements: list[dict[str, str]], split_all_elements: bool = False, kind: str = "agent",
           retry_of: str | None = None, feedback: str = "", candidate_model: str | None = None) -> dict[str, Any]:
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
    source_asset_id = output["asset_id"]
    logo_asset_id = ""
    if is_series_task(task):
        from app.services import poster_series

        logo_asset_id, _ = poster_series.series_logo(db, task)
        if version.get("series_base_asset_id"):
            source_asset_id = str(version["series_base_asset_id"])
            if db.assets.find_one({"_id": source_asset_id}) is None:
                raise StudioError("source_image_missing", "来源底图不存在", 404)
    _reject_series_logo_replacement(task, replacements, official_logo_id=logo_asset_id)
    association = {"whole_task_id": task_id, "source_version_id": source_version_id,
                   "source_output_id": source_output_id, "source_version": version["version"]}
    if kind == "model":
        association.update(kind=kind, retry_of=retry_of, feedback=feedback,
                           candidate_model=gateway_model_name(candidate_model) or None)
    return original_replacement_runner.create_task(
        db, source_asset_id=source_asset_id, whole_task_id=task_id, replacements=replacements,
        split_all_elements=kind == "agent" and split_all_elements, metadata=association,
        logo_reference_asset_id=logo_asset_id,
        candidate_model=str(association.get("candidate_model") or ""))


def retry_model_batch(db: Any, task_id: str, replacement_id: str, feedback: str) -> dict[str, Any]:
    previous = get_task(db, task_id, replacement_id)
    if previous.get("kind") != "model" or previous["status"] in original_replacement_runner.ACTIVE:
        raise StudioError("batch_not_ready", "请等待本批模型处理结束", 409)
    created = create(db, task_id, source_version_id=previous["source_version_id"],
                     source_output_id=previous["source_output_id"], replacements=previous["input"]["replacements"],
                     kind="model", retry_of=replacement_id, feedback=feedback,
                     candidate_model=str(previous.get("applied_candidate_model") or previous.get("candidate_model") or ""))
    overlay = previous.get("logo_overlay")
    if isinstance(overlay, dict) and overlay.get("box"):
        # The base poster is unchanged, so the old Logo sits where the first batch found it.
        db.background_replace_tasks.update_one(
            {"_id": created["_id"], "logo_overlay": {"$exists": False}}, {"$set": {"logo_overlay": overlay}})
        return db.background_replace_tasks.find_one({"_id": created["_id"]})
    return created


def queue_model_candidate_retry(db: Any, task_id: str, replacement_id: str, model: str, *, for_crop: bool = False) -> dict[str, Any]:
    batch = get_task(db, task_id, replacement_id)
    candidates = batch.get("candidates", [])
    index = next((i for i, item in enumerate(candidates) if item["model"] == model), None)
    if batch.get("kind") != "model" or index is None:
        raise StudioError("candidate_missing", "找不到这张模型候选", 404)
    candidate = original_replacement_runner.model_candidate(model, batch["source"])
    if candidate["status"] == "unsupported" and not for_crop:
        raise StudioError("unsupported_format", candidate["error"], 422)
    if batch["status"] == "queued":
        raise StudioError("batch_not_ready", "请等待本批模型开始处理", 409)
    field = f"candidates.{index}"
    current = candidates[index]["status"]
    terminal_failure = batch["status"] in {"failed", "interrupted"}
    if current not in {"idle", "failed", "unsupported"} and not (terminal_failure and current in original_replacement_runner.ACTIVE):
        raise StudioError("candidate_not_retryable", "只能重试未生成或失败的模型", 409)

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
    values[field] = {"model": model, "status": "queued", **({"for_crop": True} if for_crop else {})}
    claimed = db.background_replace_tasks.find_one_and_update(query, {"$set": values}, return_document=ReturnDocument.AFTER)
    if claimed is None:
        raise StudioError("candidate_changed", "候选状态已变化，请稍后重试", 409)
    return claimed


def _model_prompt(batch: dict[str, Any]) -> str:
    rows = _photo_replacements(batch)
    lines = ["图片 1 是唯一的海报底稿。保持原画布比例、取景、标题、文字、排版、装饰和未指定区域。"
             "逐字保留图片 1 的标题、副标题和其他设计文案。只从替换原照提取指定摄影内容，"
             "不得把替换原照上的文案、Logo 或排版带入海报。输出完整海报。"]
    for index, item in enumerate(rows, start=2):
        target = item["target"] or "按素材内容匹配底稿中对应的对象或区域"
        lines.append(f"图片 {index} 是替换原照。目标：{target}。")
    lines.extend([
        "目标为菜品时：以底稿中食物本身为准，锁定中心位置、可见宽高与画面占比。"
        "盘子的尺寸不能代替食物尺寸；禁止将食物缩小、放大或挪位。保留原照的食材、形状、摆盘和质感，"
        "禁止重绘、增减食材或改换餐具。仅允许轻微调整光色、边缘和接触阴影以融入底稿。",
        "目标为场景时：保留原照的主要场景特征，可调整取景、透视与光色来融合。"
        "明确指定「背景」时可替换整张摄影底图，同时保留原海报的文字、排版和装饰。",
    ])
    overlay = batch.get("logo_overlay")
    erase_box = overlay.get("erase_box") if isinstance(overlay, dict) else None
    if isinstance(overlay, dict) and overlay.get("box"):
        if erase_box:
            source_w = max(1, int(batch["source"]["width"]))
            source_h = max(1, int(batch["source"]["height"]))
            left = round(erase_box["x"] / source_w * 100)
            right = round((erase_box["x"] + erase_box["width"]) / source_w * 100)
            top = round(erase_box["y"] / source_h * 100)
            bottom = round((erase_box["y"] + erase_box["height"]) / source_h * 100)
            lines.append(
                f"移除图片 1 中原有的品牌 Logo/商标标识及其紧邻底衬（位于画面约左 {left}%–{right}%、"
                f"上 {top}%–{bottom}% 区域），用周围背景纹理与光影自然无痕补齐；"
                "严禁误删海报主标题、副标题和正文艺术字；不要在画面任何位置绘制任何品牌 Logo 或商标标识。")
        else:
            lines.append("若图片 1 中存在原有的小型品牌 Logo/商标标识，请将其移除并用周围背景自然补齐"
                         "（严禁误删海报主标题和正文艺术字）；不要在画面任何位置绘制任何品牌 Logo 或商标标识。")
    elif batch.get("logo_reference"):
        lines.append(f"图片 {len(rows) + 2} 是官方 Logo，仅用于识别品牌标识。"
                     + original_replacement_runner.SERIES_LOGO_OUTPUT_RULE)
    elif any(item["target"].casefold() == "logo" for item in batch["replacements"]):
        lines.append("Logo 使用对应的官方原文件。在底稿 Logo 的原中心、原显示框内等比放置，"
                     "保留完整图案、文字、颜色和比例，禁止拉伸、压扁、裁切或重绘。")
    else:
        lines.append("保留底稿上的 Logo 或原有的无 Logo 状态。")
    if batch.get("feedback"):
        lines.append(f"本轮反馈：{batch['feedback']}。按反馈修正指定问题，仍以图片 1 为大小、位置和排版的基准。")
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

    parent = db.whole_tasks.find_one({"_id": batch.get("whole_task_id")}) or {}
    overlay = _logo_overlay(db, settings, store, client, batch, parent)
    if overlay is not None:
        batch["logo_overlay"] = overlay
    references = []
    images = _model_input_images(batch)
    prompt = _model_prompt(batch)
    width, height = batch["source"]["width"], batch["source"]["height"]
    size = gateway_image_size(width, height)
    review_required = not is_series_task(parent)

    def run_candidate(index: int, model: str) -> None:
        for_crop = batch["candidates"][index].get("for_crop", False)
        model_input = {"channel": "gateway", "provider": "core-ai", "endpoint": "/images/edits",
                       "model": model, "prompt": prompt, "images": images,
                       "parameters": {"size": size, "quality": settings.core_ai_image_quality,
                                      "n": 1, "output_format": "png"}}
        candidate = {"model": model, "status": "running", "model_input": model_input,
                     "started_at": whole_image.utcnow()}
        if for_crop:
            candidate["for_crop"] = True
        field = f"candidates.{index}"
        claimed = tasks.update_one({"_id": batch_id, f"{field}.status": "queued"}, {"$set": {field: candidate}})
        if not claimed.matched_count:
            return
        try:
            with poster_costs.track_task(db, parent, "replace"):
                data = client.edit_image(prompt, references, size=size, model=model)
                review_error = None
                if review_required:
                    source = db.assets.find_one({"_id": batch["source"]["asset_id"]}) or {}
                    review_error = _review_model_candidate(
                        client, settings, _poster_data_url(store, source), data, remove_logo=overlay is not None,
                    )
            if review_error:
                candidate.update(status="failed", error=review_error, review_failed=True)
            else:
                output = whole_image._save_output_asset(
                    db, settings, store, task_id=batch_id, version=1, role=model,
                    data=data, target_size=None if for_crop else (width, height), allow_letterbox=False)
                candidate.update(status="succeeded", output=output)
        except Exception as exc:  # noqa: BLE001 - the other candidates keep running
            candidate.update(status="failed", error=whole_image._error_text(exc))
        now = whole_image.utcnow()
        candidate["completed_at"] = now
        tasks.update_one({"_id": batch_id}, {"$set": {field: candidate, "updated_at": now}})

    try:
        logo_reference = batch.get("logo_reference")
        for item in images:
            references.append(image_ref_from_asset(
                db, store, item["asset_id"],
                use_original=bool(logo_reference and item["asset_id"] == logo_reference["asset_id"]),
            ))
    except Exception as exc:  # noqa: BLE001 - report errors loading shared inputs
        error = whole_image._error_text(exc)
        for index, _ in candidates:
            field = f"candidates.{index}"
            tasks.update_one({"_id": batch_id, f"{field}.status": "queued"},
                             {"$set": {f"{field}.status": "failed", f"{field}.error": error}})
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
          expected_current_output_id: str, candidate_model: str | None = None,
          settings: Settings | None = None, store: ObjectStore | None = None,
          composited_png_base64: str | None = None,
          logo_box: dict[str, Any] | Any | None = None) -> dict[str, Any]:
    current = _task(db, task_id)
    replacement = db.background_replace_tasks.find_one({"_id": replacement_id, "whole_task_id": task_id})
    if replacement is None:
        raise StudioError("replacement_missing", "原照替换任务不属于当前海报", 404)
    _reject_series_logo_replacement(
        current, replacement.get("replacements") or (replacement.get("input") or {}).get("replacements")
    )
    candidate = None
    application_key = replacement_id
    composited_png: bytes | None = None
    logo_box_value = logo_box.model_dump() if hasattr(logo_box, "model_dump") else logo_box
    if isinstance(logo_box_value, dict):
        # A Logo may legally hang past the left/top edge, so x and y keep their sign.
        logo_box_value = {"x": int(logo_box_value.get("x") or 0), "y": int(logo_box_value.get("y") or 0),
                          "width": int(logo_box_value.get("width") or 0),
                          "height": int(logo_box_value.get("height") or 0)}
        if logo_box_value["width"] <= 0 or logo_box_value["height"] <= 0:
            raise StudioError("invalid_logo_box", "Logo 位置无效，请重新调整", 422)
    else:
        logo_box_value = None
    if replacement.get("kind") == "model":
        candidate = next((item for item in replacement["candidates"] if item["model"] == candidate_model), None)
        if candidate is None:
            raise StudioError("candidate_required", "请选择本批次的模型候选", 422)
        if candidate.get("for_crop"):
            raise StudioError("crop_required", "请裁剪后导出此候选", 409)
        base_key = f"{replacement_id}_{candidate['model'].replace('.', '_')}"
        output = candidate.get("output") if candidate["status"] == "succeeded" else None
        if composited_png_base64:
            if not output:
                raise StudioError("result_not_ready", "替换结果尚未成功，请先查询并确认结果", 409)
            composited_png = _decode_composited_png(
                composited_png_base64, (int(output["width"]), int(output["height"])))
            # Every tweak is its own version, so the digest joins the idempotency key.
            application_key = f"{base_key}_{hashlib.sha256(composited_png).hexdigest()[:12]}"
        else:
            application_key = base_key
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
        # The client already drew the official Logo onto the clean base; keep that PNG as the result.
        result = output
        if composited_png is not None:
            if settings is None or store is None:
                raise StudioError("composited_runtime_missing", "合成图片运行时缺失，请重启服务", 500)
            result = whole_image._save_output_asset(
                db, settings, store, task_id=replacement_id, version=1,
                role=f"{candidate['model']}_logo", data=composited_png,
                target_size=(int(output["width"]), int(output["height"])), allow_letterbox=False)
        images = original_replacement_runner.input_images(replacement)
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
            parent_version_id=source["_id"], output_asset_id=result["asset_id"], instruction=instruction,
            operation="original_replace", width=result["width"], height=result["height"], model_input=model_input,
            outputs=[{"id": "original_replace_result", "channel": model_input["channel"], "role": "original_replace_result",
                      "label": "原照替换结果", "asset_id": result["asset_id"], "width": result["width"],
                      "height": result["height"], "selectable": True, "model_input": model_input,
                      "artifacts": replacement.get("artifacts") or [], "created_at": now}])
        version.update(_id=version_id, replacement_task_id=replacement_id,
                       source_output_id=replacement["source_output_id"], replacements=replacement["input"]["replacements"])
        if isinstance(current.get("series"), dict):
            from app.services import poster_series as series_service

            if settings is None or store is None:
                raise StudioError("series_runtime_missing", "系列图配置缺失，请重启服务", 500)
            base_output = {
                "id": "original_replace_result",
                "channel": model_input["channel"],
                "role": "original_replace_result",
                "label": "原照替换结果",
                "asset_id": result["asset_id"],
                "width": result["width"],
                "height": result["height"],
                "selectable": True,
                "model_input": model_input,
                "artifacts": replacement.get("artifacts") or [],
                "created_at": now,
            }
            canvas = dict(current.get("canvas") or {}) or {
                "width": result["width"], "height": result["height"],
            }
            final_asset_id, final_outputs, series_fields = series_service.finalize_series_outputs(
                db, settings, store, task=current, version=version["version"],
                outputs=[base_output], canvas=canvas,
            )
            version["output_asset_id"] = final_asset_id
            version["outputs"] = final_outputs
            version.update(series_fields)
            version["series_style_reference_ids"] = list(
                source.get("series_style_reference_ids")
                or (current.get("series") or {}).get("style_reference_ids") or []
            )
            version["series_cover_reference"] = (
                source.get("series_cover_reference")
                or (current.get("series") or {}).get("cover_reference")
            )
            series_service.attach_series_refs(db, task_id, version)
            current_output_id = next(
                item["id"] for item in final_outputs if item.get("selectable")
            )
        else:
            current_output_id = "original_replace_result"
        db.whole_versions.insert_one(version)
        inserted = True
        committed = db.whole_tasks.update_one(
            {"_id": task_id, "edit.original_apply_token": token},
            {"$set": {"selected_version_id": version_id, "current_version_id": version_id,
                      "current_version": version["version"], "current_output_id": current_output_id,
                      "output_asset_id": version["output_asset_id"],
                      "canvas": {"width": result["width"], "height": result["height"]},
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
    _mark_applied(db, replacement_id, version,
                  composited_output=result if composited_png is not None else None, logo_box=logo_box_value)
    whole_image._append_message(db, task_id, role="assistant", content=f"已应用原照替换，保存为 v{version['version']}。",
                                version_id=version_id, version=version["version"])
    return _task(db, task_id)


def _mark_applied(db: Any, replacement_id: str, version: dict[str, Any], *,
                  composited_output: dict[str, Any] | None = None,
                  logo_box: dict[str, Any] | None = None) -> None:
    tasks = db.background_replace_tasks
    values = {"applied_version_id": version["_id"], "applied_version": version["version"]}
    model_input = version.get("model_input") or {}
    if model_input.get("channel") == "gateway":
        model = model_input["model"]
        task = tasks.find_one({"_id": replacement_id})
        index, candidate = next((i, item) for i, item in enumerate(task["candidates"]) if item["model"] == model)
        extra = {f"candidates.{index}.{key}": value for key, value in values.items()}
        # Keep the composited PNG and the tweaked box so reopening "调整 Logo" starts clean.
        if composited_output is not None:
            extra[f"candidates.{index}.composited_output"] = composited_output
        if logo_box is not None:
            extra[f"candidates.{index}.logo_box"] = logo_box
        tasks.update_one({"_id": replacement_id}, {"$set": extra})
        values.update(applied_candidate_model=model,
                      output=composited_output or candidate.get("composited_output") or candidate["output"])
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
