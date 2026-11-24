from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse

from app.api_paths import asset_thumb_url, asset_url, whole_download_url, merchant_base
from app.request_context import request_db, request_merchant_id, request_store, request_profile
from app.schemas import (gateway_model_name, PosterFinalizationRequest, WholeImageEditRequest, WholeImageTaskCreate, WholeImageWorkspaceTitle, WholeImageOriginalReplacementRequest,
                         WholeImageOriginalReplacementApplyRequest, WholeImageOriginalReplacementFeedbackRequest)
from app.services import (original_replacement_runner, brand_rules, brand_style, merchant_profile, original_replacement, platform_presets, poster_costs, poster_series, request_assets, whole_image)
from app.services.agent_image import artifacts_out as _artifacts_out
from app.services.core_ai import CoreAIError
from app.services.object_store import ObjectStoreError
from app.services.whole_image import _version_outputs as _version_outputs_for_task
from app.time_utils import utc_iso
from app.services import poster_finalizations
from app.services.canvas import canvas_size, image_model_supports_canvas

router = APIRouter(prefix="/whole-image", tags=["whole-image"])

TASK_GROUP_STATUSES = {"all", "active", "ready", "failed"}


def _edit_state(doc: dict[str, Any]) -> dict[str, Any]:
    edit = doc.get("edit")
    if not isinstance(edit, dict):
        return {
            "status": "idle",
            "image_model": None,
            "instruction": None,
            "source_version_id": None,
            "source_output_id": None,
            "reference_asset_ids": [],
            "reference_mode": "legacy",
            "error": None,
        }
    return {
        "status": edit.get("status") or "idle",
        "image_model": edit.get("image_model"),
        "instruction": edit.get("instruction"),
        "source_version_id": edit.get("source_version_id"),
        "source_output_id": edit.get("source_output_id"),
        "reference_asset_ids": list(edit.get("reference_asset_ids") or []),
        "reference_mode": edit.get("reference_mode") or "legacy",
        "error": edit.get("error"),
    }


def _task_input(doc: dict[str, Any]) -> dict[str, Any]:
    value = doc.get("input")
    return value if isinstance(value, dict) else {}


def _task_group_key(doc: dict[str, Any]) -> str:
    return poster_finalizations.group_key(doc)


def _task_is_active(doc: dict[str, Any]) -> bool:
    edit = doc.get("edit")
    edit_status = edit.get("status") if isinstance(edit, dict) else None
    return doc.get("status") in {"queued", "running"} or edit_status == "running"


def _task_is_retryable(doc: dict[str, Any]) -> bool:
    edit = doc.get("edit")
    edit_status = edit.get("status") if isinstance(edit, dict) else None
    return doc.get("status") == "failed" or edit_status == "failed"


def _task_error_message(doc: dict[str, Any]) -> str | None:
    edit = doc.get("edit")
    if isinstance(edit, dict) and edit.get("status") == "failed":
        message = str(edit.get("error") or "").strip()
        if message:
            return message
    error = doc.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or "").strip()
        if message:
            return message
    return None


def _candidate_summary(doc: dict[str, Any]) -> dict[str, Any]:
    input_data = _task_input(doc)
    output_id = doc.get("output_asset_id")
    version = int(doc.get("current_version") or 0)
    version_id = doc.get("selected_version_id") or doc.get("current_version_id")
    edit = doc.get("edit")
    edit_status = edit.get("status") if isinstance(edit, dict) else None
    merchant_id = str(doc.get("merchant_id") or "")
    return {
        "id": doc["_id"],
        "candidate_index": int(input_data.get("candidate_index") or 0),
        "image_model": input_data.get("image_model") or (doc.get("provider") or {}).get("model"),
        "status": str(doc.get("status") or "failed"),
        "stage": str(doc.get("stage") or "failed"),
        "progress": int(doc.get("progress") or 0),
        "current_version": version,
        "preview_url": asset_url(merchant_id, str(output_id), version=version) if output_id else None,
        # List screens draw a ~200 px card; the thumbnail keeps a 1080x1350 poster from
        # downloading as a multi-megabyte PNG just to fill it.
        "thumb_url": asset_thumb_url(merchant_id, str(output_id)) if output_id else None,
        "download_url": (
            whole_download_url(merchant_id, str(doc["_id"]), str(version_id))
            if output_id and version_id
            else None
        ),
        "edit_status": str(edit_status or "idle"),
        "error": _task_error_message(doc),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)


def _task_sort_key(doc: dict[str, Any]) -> tuple[int, datetime, str]:
    return (
        int(_task_input(doc).get("candidate_index") or 0),
        _as_utc(doc.get("created_at")),
        str(doc["_id"]),
    )


def _task_group_summary(group_id: str, tasks: list[dict[str, Any]], first_candidate: dict[str, Any]) -> dict[str, Any]:
    tasks = sorted(tasks, key=_task_sort_key)
    active_count = sum(1 for doc in tasks if _task_is_active(doc))
    succeeded_count = sum(1 for doc in tasks if doc.get("status") == "succeeded")
    failed_count = sum(1 for doc in tasks if doc.get("status") == "failed")
    retryable_count = sum(1 for doc in tasks if _task_is_retryable(doc))
    if active_count:
        group_status = "active"
    elif succeeded_count:
        group_status = "ready"
    else:
        group_status = "failed"
    representative = next(
        (doc for doc in tasks if doc.get("status") == "succeeded"),
        None,
    )
    if representative is None:
        representative = next(
            (doc for doc in tasks if _task_is_active(doc)),
            tasks[0],
        )
    candidates = [_candidate_summary(doc) for doc in tasks]
    representative_candidate = next(
        (item for item in candidates if item["id"] == representative["_id"]),
        candidates[0],
    )
    first_input = _task_input(tasks[0])
    brand = tasks[0].get("brand")
    platform = next((doc.get("platform") for doc in tasks if doc.get("platform")), None)
    placement = next(
        (doc.get("placement") for doc in tasks if doc.get("placement")),
        None,
    )
    canvas = next(
        (doc.get("canvas") for doc in tasks if isinstance(doc.get("canvas"), dict)),
        None,
    ) or {"width": 0, "height": 0}
    error_message = next(
        (
            message
            for message in (_task_error_message(doc) for doc in tasks)
            if message
        ),
        None,
    )
    created_at = _as_utc(first_candidate.get("created_at"))
    updated_at = max((_as_utc(doc.get("updated_at")) for doc in tasks))
    return {
        "id": group_id,
        "title": first_candidate.get("workspace_initial_title"),
        "status": group_status,
        "prompt": str(first_input.get("prompt") or ""),
        "brand_name": str((brand or {}).get("name") or "") or None,
        "platform_name": str((platform or {}).get("name") or "") or None,
        "placement_name": str((placement or {}).get("name") or "") or None,
        "canvas": {
            "width": int(canvas.get("width") or 0),
            "height": int(canvas.get("height") or 0),
        },
        "candidate_count": len(tasks),
        "image_models": list(dict.fromkeys(
            str(model) for doc in tasks
            if (model := _task_input(doc).get("image_model") or (doc.get("provider") or {}).get("model"))
        )),
        "active_count": active_count,
        "succeeded_count": succeeded_count,
        "failed_count": failed_count,
        "retryable_count": retryable_count,
        "representative_task_id": str(representative["_id"]),
        "preview_url": representative_candidate["preview_url"],
        "thumb_url": representative_candidate.get("thumb_url"),
        "download_url": representative_candidate["download_url"],
        "error_message": error_message,
        "candidates": candidates,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _workspace_start_key(doc: dict[str, Any]) -> tuple[datetime, str]:
    return _as_utc(doc.get("created_at")), str(doc["_id"])


def _build_task_groups(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for doc in docs:
        grouped.setdefault(_task_group_key(doc), []).append(doc)
    items = []
    for group_id, group_docs in grouped.items():
        visible_docs = [doc for doc in group_docs if not doc.get("candidate_deleted_at")]
        if visible_docs:
            items.append(_task_group_summary(group_id, visible_docs, min(group_docs, key=_workspace_start_key)))
    items.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)
    return items


def _list_task_groups(db: Any, *, exclude_series: bool = False) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"series": {"$exists": False}} if exclude_series else {}
    groups = _build_task_groups(list(db.whole_tasks.find(query).sort("created_at", -1)))
    titles = {doc["workspace_id"]: doc["title"] for doc in db.whole_workspace_titles.find(
        {"workspace_id": {"$in": [group["id"] for group in groups]}}, {"workspace_id": 1, "title": 1},
    )}
    for group in groups:
        group["title"] = titles.get(group["id"], group["title"])
    return groups


def _encode_task_cursor(item: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "created_at": item["created_at"].isoformat(),
            "group_id": item["id"],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_task_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        group_id = str(payload["group_id"])
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="任务游标无效") from exc
    return _as_utc(created_at), group_id


def _version_for_task(
    db: Any,
    task_id: str,
    version_id: str | None = None,
) -> dict[str, Any] | None:
    if version_id:
        return db.whole_versions.find_one({"_id": version_id, "task_id": task_id})
    task = db.whole_tasks.find_one({"_id": task_id})
    if task is None:
        return None
    selected = task.get("selected_version_id") or task.get("current_version_id")
    if selected:
        found = db.whole_versions.find_one({"_id": selected, "task_id": task_id})
        if found is not None:
            return found
    return db.whole_versions.find_one({"task_id": task_id}, sort=[("version", -1)])


def _model_input_out(db: Any, snapshot: Any) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        return None
    images = []
    for image in snapshot.get("images") or []:
        if not isinstance(image, dict):
            continue
        asset_id = str(image.get("asset_id") or "")
        asset = db.assets.find_one({"_id": asset_id}) if asset_id else None
        item = {
            "asset_id": asset_id,
            "name": str(image.get("name") or "参考图片"),
            "role": str(image.get("role") or image.get("kind") or "reference"),
            "preview_url": asset_url(str(asset.get("merchant_id") or ""), asset_id) if asset else None,
            "thumb_url": asset_thumb_url(str(asset.get("merchant_id") or ""), asset_id) if asset else None,
        }
        if image.get("kind"):
            item["kind"] = image.get("kind")
        if image.get("url"):
            item["url"] = image.get("url")
        images.append(item)
    return {
        "channel": str(snapshot.get("channel") or "gateway"),
        "endpoint": str(snapshot.get("endpoint") or ""),
        "model": str(snapshot.get("model") or ""),
        "prompt": str(snapshot.get("prompt") or ""),
        "parameters": snapshot.get("parameters") or {},
        "images": images,
    }


def _output_out(
    db: Any,
    *,
    task_id: str,
    version_id: str | None,
    version_number: int,
    output: dict[str, Any],
) -> dict[str, Any]:
    asset_id = str(output.get("asset_id") or "")
    asset = db.assets.find_one({"_id": asset_id}) if asset_id else None
    output_id = str(output.get("id") or "")
    merchant_id = str((asset or {}).get("merchant_id") or "")
    return {
        "id": output_id,
        "channel": str(output.get("channel") or "gateway"),
        "role": str(output.get("role") or "gateway_result"),
        "label": str(output.get("label") or "接口结果"),
        "asset_id": asset_id,
        "width": int(output.get("width") or (asset or {}).get("width") or 0),
        "height": int(output.get("height") or (asset or {}).get("height") or 0),
        "selectable": bool(output.get("selectable")),
        "preview_url": asset_url(merchant_id, asset_id, version=version_number) if asset_id and asset else None,
        # Small WebP for candidate strips and output grids; downloads keep using preview_url.
        "thumb_url": asset_thumb_url(merchant_id, asset_id) if asset_id and asset else None,
        "download_url": (
            whole_download_url(merchant_id, task_id, version_id, output_id)
            if asset_id and asset and version_id and output_id
            else None
        ),
        "model_input": _model_input_out(db, output.get("model_input")),
        "artifacts": _artifacts_out(output, merchant_id),
    }


def _version_outputs_out(
    db: Any,
    *,
    task_id: str,
    version_id: str,
    version_number: int,
    outputs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        _output_out(
            db,
            task_id=task_id,
            version_id=version_id,
            version_number=version_number,
            output=output,
        )
        for output in outputs
    ]


def _branch_out(db: Any, task_id: str, branch: Any) -> dict[str, Any] | None:
    if not isinstance(branch, dict):
        return None
    outputs = []
    for output in branch.get("outputs") or []:
        if isinstance(output, dict):
            outputs.append(
                _output_out(
                    db,
                    task_id=task_id,
                    version_id=None,
                    version_number=0,
                    output=output,
                )
            )
    return {
        "status": str(branch.get("status") or "idle"),
        "started_at": utc_iso(branch.get("started_at")),
        "completed_at": utc_iso(branch.get("completed_at")),
        "run_id": branch.get("run_id"),
        "error": branch.get("error"),
        "model_input": _model_input_out(db, branch.get("model_input")),
        "outputs": outputs,
    }


def task_out(db: Any, doc: dict[str, Any]) -> dict[str, Any]:
    task_id = str(doc["_id"])
    version_doc = _version_for_task(db, task_id)
    version_id = str(version_doc["_id"]) if version_doc else None
    version_number = int((version_doc or {}).get("version") or doc.get("current_version") or 0)
    raw_outputs = _version_outputs_for_task(version_doc) if version_doc else []
    outputs = (
        _version_outputs_out(
            db,
            task_id=task_id,
            version_id=version_id,
            version_number=version_number,
            outputs=raw_outputs,
        )
        if version_id
        else []
    )
    current_output_id = doc.get("current_output_id")
    selected_output = next(
        (item for item in outputs if item["id"] == current_output_id),
        None,
    )
    if selected_output is None and (doc.get("edit") or {}).get("status") != "awaiting_selection":
        selected_output = next((item for item in outputs if item["selectable"]), None)
    model_input = selected_output.get("model_input") if selected_output else None
    if model_input is None:
        snapshot = doc.get("model_input_attempt") or doc.get("model_input_latest")
        if snapshot is None and version_doc:
            snapshot = version_doc.get("model_input")
        model_input = _model_input_out(db, snapshot)
    canvas = doc.get("canvas") or {"width": 0, "height": 0}
    if version_doc:
        canvas = {
            "width": int(version_doc.get("width") or canvas.get("width") or 0),
            "height": int(version_doc.get("height") or canvas.get("height") or 0),
        }
    provider = doc.get("provider") or {}
    edit = _edit_state(doc)
    branches = (doc.get("edit") or {}).get("branches") or {}
    edit["outcome"] = (doc.get("edit") or {}).get("outcome")
    gateway_branch = _branch_out(db, task_id, branches.get("gateway"))
    edit["branches"] = {"gateway": gateway_branch} if gateway_branch is not None else {}
    return {
        "id": task_id,
        "status": doc["status"],
        "stage": doc["stage"],
        "progress": int(doc.get("progress") or 0),
        "input": doc.get("input") or {},
        "platform": doc.get("platform"),
        "placement": doc.get("placement"),
        "brand": {
            "id": (doc.get("brand") or {}).get("id"),
            "name": (doc.get("brand") or {}).get("name"),
        },
        "canvas": canvas,
        "provider": {
            "name": provider.get("name"),
            "model": provider.get("model"),
            "status": provider.get("status"),
        },
        "model_input": model_input,
        "model_inputs": [item.get("model_input") for item in outputs if item.get("model_input")],
        "outputs": outputs,
        "current_output_id": selected_output.get("id") if selected_output else None,
        "requires_output_selection": (doc.get("edit") or {}).get("status") == "awaiting_selection",
        "selected_version_id": version_id,
        "current_version_id": version_id,
        "current_version": version_number,
        "preview_url": selected_output.get("preview_url") if selected_output else None,
        "thumb_url": selected_output.get("thumb_url") if selected_output else None,
        "download_url": selected_output.get("download_url") if selected_output else None,
        "messages": [
            {**message, "created_at": utc_iso(message.get("created_at"))}
            if isinstance(message, dict)
            else message
            for message in (doc.get("messages") or [])
        ],
        "edit": edit,
        "series": doc.get("series"),
        "error": doc.get("error"),
        "created_at": utc_iso(doc["created_at"]),
        "updated_at": utc_iso(doc["updated_at"]),
    }


def _version_out(db: Any, doc: dict[str, Any]) -> dict[str, Any]:
    task_id = str(doc["task_id"])
    version_id = str(doc["_id"])
    version = int(doc.get("version") or 0)
    raw_outputs = _version_outputs_for_task(doc)
    outputs = _version_outputs_out(
        db,
        task_id=task_id,
        version_id=version_id,
        version_number=version,
        outputs=raw_outputs,
    )
    primary = next((item for item in outputs if item["selectable"]), outputs[0] if outputs else None)
    operation = doc.get("operation") or ("generate" if version <= 1 else "edit")
    return {
        "id": version_id,
        "version": version,
        "parent_version_id": doc.get("parent_version_id"),
        "operation": operation,
        "replacement_task_id": doc.get("replacement_task_id"),
        "source_output_id": doc.get("source_output_id"),
        "replacements": doc.get("replacements") or [],
        "instruction": doc.get("instruction") or "",
        "width": int(doc.get("width") or (primary or {}).get("width") or 0),
        "height": int(doc.get("height") or (primary or {}).get("height") or 0),
        "output_asset_id": primary.get("asset_id") if primary else None,
        "preview_url": primary.get("preview_url") if primary else None,
        "thumb_url": primary.get("thumb_url") if primary else None,
        "download_url": primary.get("download_url") if primary else None,
        "outputs": outputs,
        "model_input": _model_input_out(db, doc.get("model_input")),
        "created_at": utc_iso(doc["created_at"]),
    }


def _get_task_or_404(db: Any, task_id: str) -> dict[str, Any]:
    doc = db.whole_tasks.find_one({"_id": task_id})
    if doc is None or doc.get("candidate_deleted_at"):
        raise HTTPException(status_code=404, detail="整图任务不存在")
    return doc


def _require_configured(request: Request) -> None:
    settings = request.app.state.settings
    if not settings.core_ai_api_key.strip():
        raise HTTPException(status_code=503, detail="未配置 CORE_AI_API_KEY")


def _require_task_image_model(request: Request, doc: dict[str, Any]) -> None:
    """All generation and edit models use the configured gateway."""
    _require_configured(request)


def build_task_document(
    payload: WholeImageTaskCreate,
    request: Request,
) -> dict[str, Any]:
    """Validate one poster request and return the whole_tasks document to insert.

    Single-poster ``create_task`` and poster-series pages share this builder so brand,
    platform, logo and asset validation cannot drift between the two modes.
    """
    _require_configured(request)
    db = request_db(request)
    settings = request.app.state.settings

    reference_ids = None
    if payload.reference_asset_ids is not None:
        reference_ids = request_assets.resolve_asset_ids(request, payload.reference_asset_ids)
        if len(reference_ids) != len(payload.reference_asset_ids):
            raise HTTPException(status_code=422, detail="参考图不能重复")
        for reference_id in reference_ids:
            if db.assets.find_one({"_id": reference_id}) is None:
                raise HTTPException(status_code=422, detail="参考图不存在")

    product_ids = list(dict.fromkeys(payload.product_asset_ids))
    if len(product_ids) != len(payload.product_asset_ids):
        raise HTTPException(status_code=422, detail="素材图不能重复")
    product_ids = request_assets.resolve_asset_ids(request, product_ids)
    for product_id in product_ids:
        product = db.assets.find_one({"_id": product_id})
        if product is None:
            raise HTTPException(status_code=422, detail="素材图不存在")
    brand_mode = payload.brand_mode or ("preset" if payload.brand_id else "none")
    profile = request_profile(request)
    store_facts = merchant_profile.store_facts(
        profile, payload.store_place_id or "",
        include_address=payload.include_store_address,
        include_phone=payload.include_store_phone,
    )
    brand: brand_style.BrandStyle | None = None
    brand_snapshot: dict[str, Any] | None = None
    style_reference_ids: list[str] = []
    if brand_mode == "preset":
        if not payload.brand_id:
            raise HTTPException(status_code=422, detail="请选择预设品牌风格")
        try:
            brand = brand_style.get_brand(db, payload.brand_id)
            brand_snapshot = brand_style.snapshot_brand(
                brand, db, request_store(request), settings,
                profile=profile, store_facts=store_facts,
            )
        except brand_style.BrandError as exc:
            raise HTTPException(status_code=422, detail=exc.message) from exc
    elif brand_mode == "reference":
        style_reference_ids = list(dict.fromkeys(payload.style_reference_asset_ids))
        if len(style_reference_ids) != len(payload.style_reference_asset_ids):
            raise HTTPException(status_code=422, detail="风格参考图不能重复")
        style_reference_ids = request_assets.resolve_asset_ids(request, style_reference_ids)
        for asset_id in style_reference_ids:
            asset = db.assets.find_one({"_id": asset_id})
            if asset is None:
                raise HTTPException(status_code=422, detail="风格参考图不存在")

    logo_asset_id = payload.logo_asset_id
    if logo_asset_id:
        logo_asset_id = request_assets.resolve_asset_id(request, logo_asset_id)
        logo_asset = db.assets.find_one({"_id": logo_asset_id})
        if logo_asset is None:
            raise HTTPException(status_code=422, detail="Logo 不存在")

    platform_id = payload.platform_id or (brand.platform_id if brand else None)
    placement_id = payload.placement_id or (brand.placement_id if brand else None)
    if brand is not None and brand.platform_id:
        if platform_id and platform_id != brand.platform_id:
            raise HTTPException(status_code=422, detail="品牌风格与发布平台不匹配")
        if placement_id and brand.placement_id and placement_id != brand.placement_id:
            raise HTTPException(status_code=422, detail="品牌风格与发布用途不匹配")

    platform_config: dict[str, Any] | None = None
    if platform_id:
        try:
            platform_config = platform_presets.build_task_config(
                platform_id,
                placement_id,
                ratio=payload.ratio if "ratio" in payload.model_fields_set else None,
            )
        except platform_presets.PlatformError as exc:
            raise HTTPException(status_code=422, detail=exc.message) from exc
    ratio = str((platform_config or {}).get("ratio") or payload.ratio)
    width, height = (
        (int(platform_config["canvas"]["width"]), int(platform_config["canvas"]["height"]))
        if platform_config
        else canvas_size(ratio)
    )
    if not image_model_supports_canvas(payload.image_model, width, height):
        raise HTTPException(status_code=422, detail=f"图片模型 {payload.image_model} 不支持 {ratio} 画幅")
    task_id = f"wtask_{uuid.uuid4().hex}"
    now = whole_image.utcnow()
    doc = {
        "_id": task_id,
        "status": "queued",
        "stage": "pending",
        "progress": 0,
        "input": {
            "image_model": payload.image_model,
            "prompt": payload.prompt,
            "prompt_optimized": payload.prompt_optimized,
            "ratio": ratio,
            "product_asset_ids": product_ids,
            "reference_asset_ids": reference_ids,
            "brand_id": brand.id if brand else None,
            "brand_mode": brand_mode,
            "logo_asset_id": logo_asset_id,
            "style_reference_asset_ids": style_reference_ids,
            "candidate_count": payload.candidate_count,
            "candidate_batch_id": payload.candidate_batch_id,
            "generation_workspace_id": payload.generation_workspace_id,
            "candidate_index": payload.candidate_index,
            "platform_id": platform_id,
            "placement_id": (platform_config or {}).get("placement", {}).get("id"),
        },
        "platform": (platform_config or {}).get("platform"),
        "placement": (platform_config or {}).get("placement"),
        "brand": brand_snapshot,
        "merchant_context": merchant_profile.prompt_context(profile, store_facts)
        or (brand_snapshot or {}).get("merchant_context", ""),
        "brand_rules": brand_rules.get_rules(db),
        "canvas": {"width": width, "height": height},
        "provider": {
            "name": "core-ai",
            "model": payload.image_model,
            "status": "queued",
        },
        "generation": {"status": "queued", "error": None},
        "selected_version_id": None,
        "current_version_id": None,
        "current_version": 0,
        "output_asset_id": None,
        "messages": [
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "role": "user",
                "content": payload.prompt,
                "created_at": now,
                "version_id": None,
                "version": None,
                "status": "done",
            }
        ],
        "edit": {
            "status": "idle",
            "instruction": None,
            "source_version_id": None,
            "error": None,
        },
        "error": None,
        "created_at": now,
        "updated_at": now,
    }
    if payload.workspace_title is not None:
        doc["workspace_initial_title"] = payload.workspace_title
    return doc


@router.post("/tasks", status_code=202)
def create_task(
    payload: WholeImageTaskCreate,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    db = request_db(request)
    settings = request.app.state.settings
    doc = build_task_document(payload, request)
    if (
        getattr(settings, "poster_series_enabled", False)
        and payload.generation_workspace_id
    ):
        # First accepted submission fixes the workspace mode. A series submission in the
        # same workspace is rejected here, before any task becomes visible.
        try:
            poster_series.claim_workspace_mode(db, payload.generation_workspace_id, "single")
        except poster_series.SeriesError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    db.whole_tasks.insert_one(doc)
    background_tasks.add_task(
        whole_image.run_generation,
        db,
        settings,
        request_store(request),
        request.app.state.core_ai,
        doc["_id"],
    )
    return task_out(db, doc)


@router.get("/tasks")
def list_tasks(
    request: Request,
    status: str = "all",
    limit: int = 24,
    cursor: str | None = None,
) -> dict[str, Any]:
    if status not in TASK_GROUP_STATUSES:
        raise HTTPException(
            status_code=422,
            detail="status 只支持 all、active、ready 或 failed",
        )
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit 需在 1 到 100 之间")

    db = request_db(request)
    settings = getattr(request.app.state, "settings", None)
    items = _list_task_groups(
        db, exclude_series=not getattr(settings, "poster_series_enabled", False)
    )
    if status != "all":
        items = [item for item in items if item["status"] == status]
    total = len(items)

    if cursor:
        cursor_created_at, cursor_group_id = _decode_task_cursor(cursor)
        items = [
            item
            for item in items
            if item["created_at"] < cursor_created_at
            or (
                item["created_at"] == cursor_created_at
                and item["id"] < cursor_group_id
            )
        ]

    page = items[:limit]
    next_cursor = (
        _encode_task_cursor(page[-1])
        if page and len(items) > len(page)
        else None
    )
    return {
        "items": page,
        "next_cursor": next_cursor,
        "total": total,
    }


@router.get("/batches/{batch_id}")
def get_task_batch(batch_id: str, request: Request) -> list[dict[str, Any]]:
    db = request_db(request)
    docs = list(
        db.whole_tasks.find({"input.candidate_batch_id": batch_id}).sort(
            "input.candidate_index", 1
        )
    )
    if not docs:
        raise HTTPException(status_code=404, detail="候选批次不存在")
    return [task_out(db, doc) for doc in docs if not doc.get("candidate_deleted_at")]


def _workspace_filter(workspace_id: str) -> dict[str, Any]:
    return {"$or": [
        {"input.generation_workspace_id": workspace_id},
        {"input.candidate_batch_id": workspace_id, "input.generation_workspace_id": None},
        {"_id": workspace_id, "input.generation_workspace_id": None, "input.candidate_batch_id": None},
    ]}


@router.get("/workspaces/{workspace_id}/tasks")
def get_workspace_tasks(workspace_id: str, request: Request) -> list[dict[str, Any]]:
    db = request_db(request)
    docs = list(db.whole_tasks.find(_workspace_filter(workspace_id)).sort(
        [("created_at", 1), ("input.candidate_index", 1), ("_id", 1)]
    ))
    if not docs:
        raise HTTPException(status_code=404, detail="生成工作区不存在")
    return [task_out(db, doc) for doc in docs if not doc.get("candidate_deleted_at")]


@router.get("/workspaces/{workspace_id}/finalizations")
def get_workspace_finalizations(workspace_id: str, request: Request) -> list[dict[str, Any]]:
    return poster_finalizations.list_for_workspace(request_db(request), workspace_id)


def _workspace_start(db: Any, workspace_id: str) -> dict[str, Any]:
    first = db.whole_tasks.find_one(
        _workspace_filter(workspace_id),
        {"created_at": 1, "workspace_initial_title": 1},
        sort=[("created_at", 1), ("_id", 1)],
    )
    if first is None:
        raise HTTPException(status_code=404, detail="生成工作区不存在")
    return first


@router.get("/workspaces/{workspace_id}/costs")
def get_workspace_costs(workspace_id: str, request: Request, background_tasks: BackgroundTasks,
                        details: bool = False) -> dict[str, Any]:
    db = request_db(request)
    result = poster_costs.report(db, workspace_id, details=details)
    if result["pending_count"]:
        background_tasks.add_task(poster_costs.refresh_due, db, workspace_id, request.app.state.core_ai)
    return result


@router.get("/workspaces/{workspace_id}/title")
def get_workspace_title(workspace_id: str, request: Request) -> dict[str, Any]:
    db = request_db(request)
    first = _workspace_start(db, workspace_id)
    saved = db.whole_workspace_titles.find_one({"workspace_id": workspace_id}, {"title": 1})
    return {"title": saved["title"] if saved else first.get("workspace_initial_title"), "created_at": utc_iso(first.get("created_at"))}


@router.put("/workspaces/{workspace_id}/title")
def rename_workspace(workspace_id: str, payload: WholeImageWorkspaceTitle, request: Request) -> dict[str, Any]:
    db = request_db(request)
    first = _workspace_start(db, workspace_id)
    db.whole_workspace_titles.update_one(
        {"workspace_id": workspace_id}, {"$set": {"title": payload.title}}, upsert=True,
    )
    return {"title": payload.title, "created_at": utc_iso(first.get("created_at"))}


@router.delete("/tasks/{task_id}/candidate", status_code=204)
def delete_candidate(task_id: str, request: Request) -> Response:
    db = request_db(request)
    _get_task_or_404(db, task_id)
    if db.whole_finalizations.find_one({"task_id": task_id}):
        raise HTTPException(status_code=409, detail="请先撤销或替换这张海报的定稿，再删除候选")
    deleted = db.whole_tasks.find_one_and_update({
        "_id": task_id,
        "status": {"$nin": ["queued", "running"]},
        "edit.status": {"$ne": "running"},
        "candidate_deleted_at": {"$exists": False},
    }, {"$set": {"candidate_deleted_at": whole_image.utcnow()}}, return_document=True)
    if deleted is None:
        raise HTTPException(status_code=409, detail="生成或改图中的候选不能删除，请等待完成")
    if db.whole_finalizations.find_one({"task_id": task_id}):
        db.whole_tasks.update_one({"_id": task_id, "candidate_deleted_at": deleted["candidate_deleted_at"]},
                                  {"$unset": {"candidate_deleted_at": ""}})
        raise HTTPException(status_code=409, detail="这张候选刚被定稿，请先撤销或替换定稿")
    # Keep version assets and references intact; remove only this candidate from the workspace/history.
    return Response(status_code=204)


@router.get("/tasks/{task_id}/finalization")
def get_finalization(task_id: str, request: Request, version_id: str | None = None,
                     output_id: str | None = None) -> dict[str, Any]:
    try:
        return poster_finalizations.context(request_db(request), task_id, version_id, output_id)
    except whole_image.StudioError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.put("/tasks/{task_id}/finalization")
def save_finalization(task_id: str, payload: PosterFinalizationRequest, request: Request) -> dict[str, Any]:
    try:
        return poster_finalizations.save(request_db(request), task_id, **payload.model_dump())
    except whole_image.StudioError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.delete("/tasks/{task_id}/finalization", status_code=204)
def revoke_finalization(task_id: str, expected_revision: str, request: Request) -> Response:
    try:
        poster_finalizations.revoke(request_db(request), task_id, expected_revision)
    except whole_image.StudioError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return Response(status_code=204)


@router.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request) -> dict[str, Any]:
    db = request_db(request)
    return task_out(db, _get_task_or_404(db, task_id))


@router.post("/tasks/{task_id}/retry", status_code=202)
def retry_task(
    task_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    db = request_db(request)
    doc = _get_task_or_404(db, task_id)
    _require_task_image_model(request, doc)

    retried_generation = db.whole_tasks.find_one_and_update(
        {"_id": task_id, "status": "failed", "candidate_deleted_at": {"$exists": False}},
        {
            "$set": {
                "status": "queued",
                "stage": "pending",
                "progress": 0,
                "error": None,
                "generation.status": "queued",
                "generation.error": None,
                "generation.started_at": None,
                "provider.status": "queued",
                "model_input_attempt": None,
                "updated_at": whole_image.utcnow(),
            }
        },
        return_document=True,
    )
    if retried_generation is not None:
        background_tasks.add_task(
            whole_image.run_generation,
            db,
            request.app.state.settings,
            request_store(request),
            request.app.state.core_ai,
            task_id,
        )
        return task_out(db, retried_generation)

    if _task_is_active(doc):
        raise HTTPException(status_code=409, detail="任务正在执行，请稍候")
    edit = doc.get("edit") if isinstance(doc.get("edit"), dict) else {}
    if doc.get("status") != "succeeded" or edit.get("status") != "failed":
        raise HTTPException(status_code=409, detail="当前任务没有可重试的失败项")
    instruction = str(edit.get("instruction") or "").strip()
    source_version_id = str(edit.get("source_version_id") or "").strip()
    source_output_id = str(edit.get("source_output_id") or "").strip() or None
    reference_asset_ids = [
        str(asset_id).strip()
        for asset_id in list(edit.get("reference_asset_ids") or [])
        if str(asset_id).strip()
    ]
    if not instruction or not source_version_id:
        raise HTTPException(status_code=409, detail="失败记录缺少重试所需信息")

    retried_edit = db.whole_tasks.find_one_and_update(
        {
            "_id": task_id,
            "status": "succeeded",
            "edit.status": "failed",
            "candidate_deleted_at": {"$exists": False},
        },
        {
            "$set": {
                "stage": "editing",
                "progress": 40,
                "edit.status": "running",
                "edit.error": None,
                "updated_at": whole_image.utcnow(),
            }
        },
        return_document=True,
    )
    if retried_edit is None:
        raise HTTPException(status_code=409, detail="任务状态已变化，请刷新后重试")
    background_tasks.add_task(
        whole_image.run_edit,
        db,
        request.app.state.settings,
        request_store(request),
        request.app.state.core_ai,
        task_id,
        instruction,
        source_version_id,
        source_output_id,
        reference_asset_ids,
        reference_mode=str(edit.get("reference_mode") or "legacy"),
    )
    return task_out(db, retried_edit)


@router.post("/tasks/{task_id}/edits", status_code=202)
def create_edit(
    task_id: str,
    payload: WholeImageEditRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    db = request_db(request)
    doc = _get_task_or_404(db, task_id)
    _require_task_image_model(request, doc)
    if doc.get("status") != "succeeded":
        raise HTTPException(status_code=409, detail="任务尚未生成可编辑的图片")
    reference_asset_ids = request_assets.resolve_asset_ids(request, payload.reference_asset_ids)
    if payload.reference_mode == "unified" and len(reference_asset_ids) != len(payload.reference_asset_ids):
        raise HTTPException(status_code=422, detail="参考图不能重复")
    source = db.whole_versions.find_one(
        {"_id": payload.source_version_id, "task_id": task_id}
    )
    if source is None:
        raise HTTPException(status_code=422, detail="来源版本不属于当前任务")
    reference_attachments: list[dict[str, str]] = []
    for asset_id in reference_asset_ids:
        asset = db.assets.find_one({"_id": asset_id})
        if asset is None:
            raise HTTPException(status_code=422, detail="参考图片不存在")
        reference_attachments.append(
            {
                "asset_id": asset_id,
                "name": str(asset.get("original_name") or "参考图片"),
                "url": asset.get("prepared_url") or asset.get("url") or asset_url(str(asset.get("merchant_id") or ""), asset_id),
            }
        )
    claimed = db.whole_tasks.find_one_and_update(
        {
            "_id": task_id,
            "status": "succeeded",
            "edit.status": {"$ne": "running"},
            "candidate_deleted_at": {"$exists": False},
        },
        {
            "$set": {
                "edit": {
                    "status": "running",
                    "image_model": gateway_model_name(
                        payload.image_model or _task_input(doc).get("image_model")
                        or request.app.state.settings.core_ai_image_model
                    ),
                    "instruction": payload.instruction,
                    "source_version_id": payload.source_version_id,
                    "source_output_id": payload.source_output_id,
                    "reference_asset_ids": reference_asset_ids,
                    "reference_mode": payload.reference_mode,
                    "error": None,
                },
                "stage": "editing",
                "progress": 40,
                "updated_at": whole_image.utcnow(),
            }
        },
        return_document=True,
    )
    if claimed is None:
        raise HTTPException(status_code=409, detail="已有修改正在执行，请稍候")
    whole_image._append_message(
        db,
        task_id,
        role="user",
        content=payload.instruction,
        attachments=reference_attachments,
        image_model=claimed["edit"]["image_model"],
    )
    background_tasks.add_task(
        whole_image.run_edit,
        db,
        request.app.state.settings,
        request_store(request),
        request.app.state.core_ai,
        task_id,
        payload.instruction,
        payload.source_version_id,
        payload.source_output_id,
        reference_asset_ids,
        reference_mode=payload.reference_mode,
    )
    return task_out(db, _get_task_or_404(db, task_id))


@router.post("/tasks/{task_id}/original-replacements", status_code=202)
def create_original_replacement(task_id: str, payload: WholeImageOriginalReplacementRequest,
                                request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    _require_configured(request)
    state = request.app.state
    db = request_db(request)
    store = request_store(request)
    try:
        _get_task_or_404(db, task_id)
        values = payload.model_dump()
        values["replacements"] = [dict(item, asset_id=request_assets.resolve_asset_id(request, item["asset_id"]))
                                  for item in values["replacements"]]
        doc = original_replacement.create(db, task_id, **values)
    except (whole_image.StudioError, original_replacement_runner.OriginalReplacementError) as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    run = original_replacement.run_model_batch if payload.kind == "model" else original_replacement_runner.run_task
    background_tasks.add_task(run, db, state.settings, store, state.core_ai, doc["_id"])
    return original_replacement_runner.task_out(doc)


@router.post("/tasks/{task_id}/original-replacements/{replacement_id}/feedback", status_code=202)
def retry_model_replacement(task_id: str, replacement_id: str, payload: WholeImageOriginalReplacementFeedbackRequest,
                            request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    _require_configured(request)
    state = request.app.state
    db = request_db(request)
    _get_task_or_404(db, task_id)
    try:
        doc = original_replacement.retry_model_batch(db, task_id, replacement_id, payload.feedback)
    except (whole_image.StudioError, original_replacement_runner.OriginalReplacementError) as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    background_tasks.add_task(original_replacement.run_model_batch, db, state.settings,
                              request_store(request), state.core_ai, doc["_id"])
    return original_replacement_runner.task_out(doc)


@router.post("/tasks/{task_id}/original-replacements/{replacement_id}/candidates/{candidate_model}/retry", status_code=202)
def retry_original_replacement_candidate(task_id: str, replacement_id: str, candidate_model: str,
                                         request: Request, background_tasks: BackgroundTasks, for_crop: bool = False) -> dict[str, Any]:
    _require_configured(request)
    state = request.app.state
    db = request_db(request)
    _get_task_or_404(db, task_id)
    try:
        doc = original_replacement.queue_model_candidate_retry(db, task_id, replacement_id, candidate_model, for_crop=for_crop)
    except whole_image.StudioError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    background_tasks.add_task(original_replacement.run_model_batch, db, state.settings,
                              request_store(request), state.core_ai, doc["_id"], candidate_model)
    return original_replacement_runner.task_out(doc)


@router.get("/tasks/{task_id}/original-replacements/{replacement_id}/candidates/{candidate_model}/crop-source")
def get_original_replacement_crop_source(task_id: str, replacement_id: str, candidate_model: str, request: Request) -> Response:
    try:
        batch = original_replacement.get_task(request_db(request), task_id, replacement_id)
    except whole_image.StudioError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    output = next((item.get("output") for item in batch.get("candidates", [])
                   if item["model"] == candidate_model and item.get("for_crop") and item["status"] == "succeeded"), None)
    if not output:
        raise HTTPException(status_code=404, detail="裁剪图片尚未就绪")
    data = _artifact_bytes(request_store(request), output)
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, no-store"})


@router.get("/tasks/{task_id}/original-replacements")
def list_original_replacements(task_id: str, request: Request) -> list[dict[str, Any]]:
    try:
        return [original_replacement_runner.task_out(doc) for doc in original_replacement.list_tasks(request_db(request), task_id)]
    except whole_image.StudioError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/tasks/{task_id}/original-replacements/{replacement_id}/apply")
def apply_original_replacement(task_id: str, replacement_id: str, payload: WholeImageOriginalReplacementApplyRequest,
                               request: Request) -> dict[str, Any]:
    try:
        doc = original_replacement.apply(request_db(request), task_id, replacement_id, **payload.model_dump(),
                                         settings=request.app.state.settings, store=request_store(request))
    except whole_image.StudioError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return task_out(request_db(request), doc)


@router.post("/tasks/{task_id}/original-replacements/{replacement_id}/refresh-result", status_code=202)
def refresh_original_replacement(task_id: str, replacement_id: str, request: Request,
                                 background_tasks: BackgroundTasks) -> dict[str, Any]:
    _require_configured(request)
    state = request.app.state
    db = request_db(request)
    store = request_store(request)
    try:
        original_replacement.get_task(db, task_id, replacement_id)
        doc = original_replacement_runner.queue_refresh(db, replacement_id)
    except (whole_image.StudioError, original_replacement_runner.OriginalReplacementError) as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    if doc["status"] == "queued":
        background_tasks.add_task(original_replacement_runner.run_task, db, state.settings,
                                  store, state.core_ai, replacement_id, refresh=True)
    return original_replacement_runner.task_out(doc)


@router.post("/tasks/{task_id}/original-replacements/{replacement_id}/artifacts/restore")
def restore_original_replacement_artifacts(task_id: str, replacement_id: str, request: Request) -> dict[str, Any]:
    _require_configured(request)
    db = request_db(request)
    state = request.app.state
    try:
        original_replacement.get_task(db, task_id, replacement_id)
        doc = original_replacement_runner.restore_artifacts(db, state.settings, request_store(request), state.core_ai, replacement_id)
    except (whole_image.StudioError, CoreAIError) as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return original_replacement_runner.task_out(doc)


@router.get("/tasks/{task_id}/original-replacements/{replacement_id}/download")
def download_original_replacement(task_id: str, replacement_id: str, request: Request) -> Response:
    state = request.app.state
    db = request_db(request)
    store = request_store(request)
    try:
        doc = original_replacement.get_task(db, task_id, replacement_id)
    except whole_image.StudioError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    output = doc.get("output")
    if doc["status"] != "succeeded" or not output:
        raise HTTPException(status_code=404, detail="最终结果尚未就绪")
    name = f"original-replace-{replacement_id}.png"
    local = store.local_path(output["path"])
    if local is not None:
        return FileResponse(local, media_type="image/png", filename=name)
    return RedirectResponse(store.presign(output["path"], expires=state.settings.cos_presign_expiry_seconds,
                                               download_name=name), status_code=302)


@router.get("/tasks/{task_id}/versions")
def list_versions(task_id: str, request: Request) -> list[dict[str, Any]]:
    db = request_db(request)
    _get_task_or_404(db, task_id)
    docs = db.whole_versions.find({"task_id": task_id}).sort("version", 1).limit(100)
    return [_version_out(db, doc) for doc in docs]


@router.post("/tasks/{task_id}/versions/{version_id}/select")
def select_task_version(
    task_id: str,
    version_id: str,
    request: Request,
) -> dict[str, Any]:
    db = request_db(request)
    _get_task_or_404(db, task_id)
    try:
        doc = whole_image.select_version(db, task_id, version_id)
    except whole_image.StudioError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return task_out(db, doc)


@router.post("/tasks/{task_id}/versions/{version_id}/outputs/{output_id}/select")
def select_task_output(
    task_id: str,
    version_id: str,
    output_id: str,
    request: Request,
) -> dict[str, Any]:
    db = request_db(request)
    _get_task_or_404(db, task_id)
    try:
        doc = whole_image.select_output(db, task_id, version_id, output_id)
    except whole_image.StudioError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return task_out(db, doc)


@router.get("/tasks/{task_id}/download")
def download_task(
    task_id: str,
    request: Request,
    version_id: str | None = None,
    output_id: str | None = None,
) -> Response:
    db = request_db(request)
    _get_task_or_404(db, task_id)
    selected = _version_for_task(db, task_id, version_id=version_id)
    selected_output: dict[str, Any] | None = None
    if selected and output_id:
        selected_output = next(
            (item for item in _version_outputs_for_task(selected) if str(item.get("id")) == output_id),
            None,
        )
        if selected_output is None:
            raise HTTPException(status_code=404, detail="结果图片不存在")
    output_asset_id = (
        selected_output.get("asset_id")
        if selected_output
        else (selected.get("output_asset_id") if selected else None)
    )
    if not output_asset_id and selected:
        first = next(
            (item for item in _version_outputs_for_task(selected) if item.get("selectable")),
            None,
        )
        output_asset_id = first.get("asset_id") if first else None
    if not output_asset_id:
        raise HTTPException(status_code=409, detail="任务尚未生成可下载的成品")
    settings = request.app.state.settings
    store = request_store(request)
    asset = db.assets.find_one({"_id": output_asset_id})
    if asset is None:
        raise HTTPException(status_code=404, detail="成品不存在")
    number = int((selected or {}).get("version") or 0)
    filename = (
        f"whole-image-{task_id}-v{number}-{selected_output.get('role') or 'result'}.png"
        if selected_output
        else f"whole-image-{task_id}-v{number}.png"
    )
    relative = asset.get("prepared_path") or asset["path"]
    local_path = store.local_path(relative)
    if local_path is not None:
        return FileResponse(
            local_path,
            media_type="image/png",
            filename=filename,
            content_disposition_type="attachment",
        )
    url = store.presign(
        relative,
        expires=settings.cos_presign_expiry_seconds,
        download_name=filename,
    )
    return RedirectResponse(url, status_code=302)


_ARTIFACT_PREVIEW_CSP = (
    "sandbox allow-scripts allow-downloads; default-src 'none'; "
    "script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; connect-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'self'"
)
ARTIFACT_PREVIEW_HEADERS = {
    "Content-Disposition": "inline",
    "Cache-Control": "private, no-store",
    "Content-Security-Policy": _ARTIFACT_PREVIEW_CSP,
    "X-Content-Type-Options": "nosniff",
}
_ARTIFACT_UNSAFE_CHARS = frozenset('"\\')


def _artifact_ascii_char(ch: str) -> str:
    if ch not in _ARTIFACT_UNSAFE_CHARS and ord(ch) >= 32 and ord(ch) < 127:
        return ch
    return "_"


def _artifact_doc(db: Any, artifact_id: str) -> dict[str, Any]:
    doc = db.whole_artifacts.find_one({"_id": artifact_id})
    if doc is None:
        raise HTTPException(status_code=404, detail="产物不存在")
    return doc


def _artifact_bytes(store: Any, doc: dict[str, Any]) -> bytes:
    key = str(doc.get("path") or "")
    if not key:
        raise HTTPException(status_code=404, detail="产物文件不存在")
    try:
        return store.get_bytes(key)
    except ObjectStoreError as exc:
        raise HTTPException(status_code=404, detail="产物文件不存在") from exc


def _artifact_content_disposition(file_name: str) -> str:
    # ASCII-only fallback plus RFC5987 filename* so Chinese artifact names survive.
    safe_name = Path(file_name).name or "artifact.bin"
    fallback = "".join(_artifact_ascii_char(ch) for ch in safe_name)
    if not fallback.strip("_"):
        fallback = "artifact.bin"
    value = f'attachment; filename="{fallback}"'
    if fallback != safe_name:
        value += f"; filename*=UTF-8''{quote(safe_name, safe='')}"
    return value


@router.get("/artifacts/{artifact_id}/preview")
def preview_artifact(artifact_id: str, request: Request) -> Response:
    doc = _artifact_doc(request_db(request), artifact_id)
    if doc.get("kind") != "html":
        raise HTTPException(status_code=422, detail="该产物不是 HTML")
    data = _artifact_bytes(request_store(request), doc)
    return Response(
        content=data, media_type="text/html", headers=ARTIFACT_PREVIEW_HEADERS
    )


@router.get("/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: str, request: Request) -> Response:
    doc = _artifact_doc(request_db(request), artifact_id)
    data = _artifact_bytes(request_store(request), doc)
    filename = str(doc.get("file_name") or f"{artifact_id}.bin")
    return Response(
        content=data,
        media_type=str(doc.get("content_type") or "application/octet-stream"),
        headers={
            "Content-Disposition": _artifact_content_disposition(filename),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
