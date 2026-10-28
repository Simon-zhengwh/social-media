from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse

from app.schemas import WholeImageEditRequest, WholeImageTaskCreate
from app.services import brand_rules, brand_style, platform_presets, whole_image
from app.services.whole_image import _version_outputs as _version_outputs_for_task
from app.time_utils import utc_iso
from app.services.canvas import canvas_size

router = APIRouter(prefix="/api/whole-image", tags=["whole-image"])

TASK_GROUP_STATUSES = {"all", "active", "ready", "failed"}


def _edit_state(doc: dict[str, Any]) -> dict[str, Any]:
    edit = doc.get("edit")
    if not isinstance(edit, dict):
        return {
            "status": "idle",
            "instruction": None,
            "source_version_id": None,
            "source_output_id": None,
            "error": None,
        }
    return {
        "status": edit.get("status") or "idle",
        "instruction": edit.get("instruction"),
        "source_version_id": edit.get("source_version_id"),
        "source_output_id": edit.get("source_output_id"),
        "error": edit.get("error"),
    }


def _task_input(doc: dict[str, Any]) -> dict[str, Any]:
    value = doc.get("input")
    return value if isinstance(value, dict) else {}


def _task_group_key(doc: dict[str, Any]) -> str:
    batch_id = str(_task_input(doc).get("candidate_batch_id") or "").strip()
    return batch_id or str(doc["_id"])


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
    return {
        "id": doc["_id"],
        "candidate_index": int(input_data.get("candidate_index") or 0),
        "status": str(doc.get("status") or "failed"),
        "stage": str(doc.get("stage") or "failed"),
        "progress": int(doc.get("progress") or 0),
        "current_version": version,
        "preview_url": (
            f"/api/assets/{output_id}/file?v={version}" if output_id else None
        ),
        "download_url": (
            f"/api/whole-image/tasks/{quote(str(doc['_id']))}/download"
            f"?version_id={quote(str(version_id))}"
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


def _task_group_summary(group_id: str, docs: list[dict[str, Any]]) -> dict[str, Any]:
    tasks = sorted(docs, key=_task_sort_key)
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
    created_at = min((_as_utc(doc.get("created_at")) for doc in tasks))
    updated_at = max((_as_utc(doc.get("updated_at")) for doc in tasks))
    return {
        "id": group_id,
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
        "active_count": active_count,
        "succeeded_count": succeeded_count,
        "failed_count": failed_count,
        "retryable_count": retryable_count,
        "representative_task_id": str(representative["_id"]),
        "preview_url": representative_candidate["preview_url"],
        "download_url": representative_candidate["download_url"],
        "error_message": error_message,
        "candidates": candidates,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _build_task_groups(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for doc in docs:
        grouped.setdefault(_task_group_key(doc), []).append(doc)
    items = [
        _task_group_summary(group_id, group_docs)
        for group_id, group_docs in grouped.items()
    ]
    items.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)
    return items


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
            "preview_url": f"/api/assets/{asset_id}/file" if asset else None,
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
    return {
        "id": output_id,
        "channel": str(output.get("channel") or "gateway"),
        "role": str(output.get("role") or "gateway_result"),
        "label": str(output.get("label") or "接口结果"),
        "asset_id": asset_id,
        "width": int(output.get("width") or (asset or {}).get("width") or 0),
        "height": int(output.get("height") or (asset or {}).get("height") or 0),
        "selectable": bool(output.get("selectable")),
        "preview_url": (
            f"/api/assets/{asset_id}/file?v={version_number}" if asset_id and asset else None
        ),
        "download_url": (
            f"/api/whole-image/tasks/{task_id}/download?version_id={version_id}&output_id={output_id}"
            if asset_id and asset and version_id and output_id
            else None
        ),
        "model_input": _model_input_out(db, output.get("model_input")),
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
    edit["branches"] = {
        name: value
        for name, value in (
            ("gateway", _branch_out(db, task_id, branches.get("gateway"))),
            ("agent", _branch_out(db, task_id, branches.get("agent"))),
        )
        if value is not None
    }
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
        "download_url": selected_output.get("download_url") if selected_output else None,
        "messages": [
            {**message, "created_at": utc_iso(message.get("created_at"))}
            if isinstance(message, dict)
            else message
            for message in (doc.get("messages") or [])
        ],
        "edit": edit,
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
        "instruction": doc.get("instruction") or "",
        "width": int(doc.get("width") or (primary or {}).get("width") or 0),
        "height": int(doc.get("height") or (primary or {}).get("height") or 0),
        "output_asset_id": primary.get("asset_id") if primary else None,
        "preview_url": primary.get("preview_url") if primary else None,
        "download_url": primary.get("download_url") if primary else None,
        "outputs": outputs,
        "model_input": _model_input_out(db, doc.get("model_input")),
        "created_at": utc_iso(doc["created_at"]),
    }


def _get_task_or_404(db: Any, task_id: str) -> dict[str, Any]:
    doc = db.whole_tasks.find_one({"_id": task_id})
    if doc is None:
        raise HTTPException(status_code=404, detail="整图任务不存在")
    return doc


def _require_configured(request: Request) -> None:
    settings = request.app.state.settings
    if not settings.core_ai_api_key.strip():
        raise HTTPException(status_code=503, detail="未配置 CORE_AI_API_KEY")


@router.post("/tasks", status_code=202)
def create_task(
    payload: WholeImageTaskCreate,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    _require_configured(request)
    db = request.app.state.db
    settings = request.app.state.settings

    product_ids = list(dict.fromkeys(payload.product_asset_ids))
    if len(product_ids) != len(payload.product_asset_ids):
        raise HTTPException(status_code=422, detail="素材图不能重复")
    for product_id in product_ids:
        product = db.assets.find_one({"_id": product_id})
        if product is None:
            raise HTTPException(status_code=422, detail="素材图不存在")
    brand_mode = payload.brand_mode or ("preset" if payload.brand_id else "none")
    brand: brand_style.BrandStyle | None = None
    brand_snapshot: dict[str, Any] | None = None
    style_reference_ids: list[str] = []
    if brand_mode == "preset":
        if not payload.brand_id:
            raise HTTPException(status_code=422, detail="请选择预设品牌风格")
        try:
            brand = brand_style.get_brand(db, payload.brand_id)
            brand_snapshot = brand_style.snapshot_brand(
                brand, db, request.app.state.store, settings
            )
        except brand_style.BrandError as exc:
            raise HTTPException(status_code=422, detail=exc.message) from exc
    elif brand_mode == "reference":
        style_reference_ids = list(dict.fromkeys(payload.style_reference_asset_ids))
        if len(style_reference_ids) != len(payload.style_reference_asset_ids):
            raise HTTPException(status_code=422, detail="风格参考图不能重复")
        for asset_id in style_reference_ids:
            asset = db.assets.find_one({"_id": asset_id})
            if asset is None:
                raise HTTPException(status_code=422, detail="风格参考图不存在")

    logo_asset_id = payload.logo_asset_id
    if logo_asset_id:
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
            "brand_id": brand.id if brand else None,
            "brand_mode": brand_mode,
            "logo_asset_id": logo_asset_id,
            "style_reference_asset_ids": style_reference_ids,
            "candidate_count": payload.candidate_count,
            "candidate_batch_id": payload.candidate_batch_id,
            "candidate_index": payload.candidate_index,
            "platform_id": platform_id,
            "placement_id": (platform_config or {}).get("placement", {}).get("id"),
        },
        "platform": (platform_config or {}).get("platform"),
        "placement": (platform_config or {}).get("placement"),
        "brand": brand_snapshot,
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
    db.whole_tasks.insert_one(doc)
    background_tasks.add_task(
        whole_image.run_generation,
        db,
        settings,
        request.app.state.store,
        request.app.state.core_ai,
        task_id,
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

    db = request.app.state.db
    docs = list(db.whole_tasks.find({}).sort("created_at", -1))
    items = _build_task_groups(docs)
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
    db = request.app.state.db
    docs = list(
        db.whole_tasks.find({"input.candidate_batch_id": batch_id}).sort(
            "input.candidate_index", 1
        )
    )
    if not docs:
        raise HTTPException(status_code=404, detail="候选批次不存在")
    return [task_out(db, doc) for doc in docs]


@router.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request) -> dict[str, Any]:
    db = request.app.state.db
    return task_out(db, _get_task_or_404(db, task_id))


@router.post("/tasks/{task_id}/retry", status_code=202)
def retry_task(
    task_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    _require_configured(request)
    db = request.app.state.db

    retried_generation = db.whole_tasks.find_one_and_update(
        {"_id": task_id, "status": "failed"},
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
            request.app.state.store,
            request.app.state.core_ai,
            task_id,
        )
        return task_out(db, retried_generation)

    doc = _get_task_or_404(db, task_id)
    if _task_is_active(doc):
        raise HTTPException(status_code=409, detail="任务正在执行，请稍候")
    edit = doc.get("edit") if isinstance(doc.get("edit"), dict) else {}
    if doc.get("status") != "succeeded" or edit.get("status") != "failed":
        raise HTTPException(status_code=409, detail="当前任务没有可重试的失败项")
    instruction = str(edit.get("instruction") or "").strip()
    source_version_id = str(edit.get("source_version_id") or "").strip()
    source_output_id = str(edit.get("source_output_id") or "").strip() or None
    if not instruction or not source_version_id:
        raise HTTPException(status_code=409, detail="失败记录缺少重试所需信息")

    retried_edit = db.whole_tasks.find_one_and_update(
        {
            "_id": task_id,
            "status": "succeeded",
            "edit.status": "failed",
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
        request.app.state.store,
        request.app.state.core_ai,
        task_id,
        instruction,
        source_version_id,
        source_output_id,
    )
    return task_out(db, retried_edit)


@router.post("/tasks/{task_id}/edits", status_code=202)
def create_edit(
    task_id: str,
    payload: WholeImageEditRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    _require_configured(request)
    db = request.app.state.db
    doc = _get_task_or_404(db, task_id)
    if doc.get("status") != "succeeded":
        raise HTTPException(status_code=409, detail="任务尚未生成可编辑的图片")
    source = db.whole_versions.find_one(
        {"_id": payload.source_version_id, "task_id": task_id}
    )
    if source is None:
        raise HTTPException(status_code=422, detail="来源版本不属于当前任务")
    claimed = db.whole_tasks.find_one_and_update(
        {
            "_id": task_id,
            "status": "succeeded",
            "edit.status": {"$ne": "running"},
        },
        {
            "$set": {
                "edit": {
                    "status": "running",
                    "instruction": payload.instruction,
                    "source_version_id": payload.source_version_id,
                    "source_output_id": payload.source_output_id,
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
    )
    background_tasks.add_task(
        whole_image.run_edit,
        db,
        request.app.state.settings,
        request.app.state.store,
        request.app.state.core_ai,
        task_id,
        payload.instruction,
        payload.source_version_id,
        payload.source_output_id,
    )
    return task_out(db, _get_task_or_404(db, task_id))


@router.get("/tasks/{task_id}/versions")
def list_versions(task_id: str, request: Request) -> list[dict[str, Any]]:
    db = request.app.state.db
    _get_task_or_404(db, task_id)
    docs = db.whole_versions.find({"task_id": task_id}).sort("version", 1).limit(100)
    return [_version_out(db, doc) for doc in docs]


@router.post("/tasks/{task_id}/versions/{version_id}/select")
def select_task_version(
    task_id: str,
    version_id: str,
    request: Request,
) -> dict[str, Any]:
    db = request.app.state.db
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
    db = request.app.state.db
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
    db = request.app.state.db
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
    store = request.app.state.store
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
