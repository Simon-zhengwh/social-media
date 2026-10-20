from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from app.schemas import EditRequest, LayerPatch, TaskCreate
from app.services import brand_style, platform_presets
from app.services import design as design_utils
from app.services.canvas import canvas_size
from app.services.pipeline import (
    _append_message,
    can_resume,
    restore_version,
    run_component_regeneration,
    run_edit,
    run_layer_update,
    run_task,
    utcnow,
)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _edit_state(doc: dict[str, Any]) -> dict[str, Any]:
    edit = doc.get("edit")
    if not isinstance(edit, dict):
        return {"status": "idle", "instruction": None, "error": None}
    return {
        "status": edit.get("status") or "idle",
        "instruction": edit.get("instruction"),
        "error": edit.get("error"),
    }


def task_out(doc: dict[str, Any]) -> dict[str, Any]:
    output_id = doc.get("output_asset_id")
    provider = doc.get("provider") or {}
    brand_snapshot = doc.get("brand") if isinstance(doc.get("brand"), dict) else None
    input_data = doc.get("input") or {}
    version = int(doc.get("current_version") or 0)
    preview_url = f"/api/assets/{output_id}/file?v={version}" if output_id else None
    return {
        "id": doc["_id"],
        "status": doc["status"],
        "stage": doc["stage"],
        "progress": int(doc.get("progress") or 0),
        "input": input_data,
        "platform": doc.get("platform"),
        "placement": doc.get("placement"),
        "brand": (
            {
                "id": brand_snapshot.get("id") or input_data.get("brand_id"),
                "name": brand_snapshot.get("name"),
                "short_name": brand_snapshot.get("short_name"),
                "summary": brand_snapshot.get("summary"),
            }
            if brand_snapshot
            else None
        ),
        "canvas": doc["canvas"],
        "provider": {
            key: provider.get(key)
            for key in ("name", "model", "task_id", "status", "progress")
        },
        "background_asset_id": doc.get("background_asset_id"),
        "background_url": (
            f"/api/assets/{doc['design']['background']['render_asset_id']}/file"
            if isinstance(doc.get("design"), dict)
            and isinstance((doc.get("design") or {}).get("background"), dict)
            and (doc.get("design") or {}).get("background", {}).get("render_asset_id")
            else None
        ),
        "design_reference_asset_id": doc.get("design_reference_asset_id"),
        "design_reference_url": (
            f"/api/assets/{doc['design_reference_asset_id']}/file"
            if doc.get("design_reference_asset_id") else None
        ),
        "reference_guided": doc.get("reference_guided") or {"enabled": False, "status": "classic"},
        "output_asset_id": output_id,
        "preview_url": preview_url,
        "download_url": f"/api/tasks/{doc['_id']}/download" if output_id else None,
        "design": doc.get("design"),
        "design_intent": (doc.get("design") or {}).get("design_intent"),
        "scene_revision": int(doc.get("scene_revision") or (doc.get("design") or {}).get("scene_revision") or 0),
        "renderer": (doc.get("design") or {}).get("renderer"),
        "review": doc.get("review") or {"status": "not_run", "issues": [], "candidates": []},
        "messages": doc.get("messages") or [],
        "current_version": version,
        "edit": _edit_state(doc),
        "quality": doc.get("quality") or {"warnings": []},
        "error": doc.get("error"),
        "created_at": doc["created_at"],
        "updated_at": doc["updated_at"],
    }


def _version_out(doc: dict[str, Any]) -> dict[str, Any]:
    output_id = doc.get("output_asset_id")
    return {
        "version": int(doc["version"]),
        "created_at": doc["created_at"],
        "instruction": doc.get("instruction") or "",
        "summary": doc.get("summary") or "",
        "output_asset_id": output_id,
        "preview_url": (
            f"/api/assets/{output_id}/file?v={int(doc['version'])}"
            if output_id
            else None
        ),
    }


def _get_task_or_404(db: Any, task_id: str) -> dict[str, Any]:
    doc = db.tasks.find_one({"_id": task_id})
    if doc is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return doc


@router.post("", status_code=202)
def create_task(
    payload: TaskCreate,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    db = request.app.state.db
    settings = request.app.state.settings
    product_ids = list(dict.fromkeys(payload.product_asset_ids))
    if len(product_ids) != len(payload.product_asset_ids):
        raise HTTPException(status_code=422, detail="参考素材不能重复")
    for product_id in product_ids:
        product = db.assets.find_one({"_id": product_id})
        if product is None or product.get("kind") not in {"product", "logo"}:
            raise HTTPException(status_code=422, detail="参考素材不存在或类型不正确")
    brand_mode = payload.brand_mode or ("preset" if payload.brand_id else "none")
    brand: brand_style.BrandStyle | None = None
    brand_snapshot: dict[str, Any] | None = None
    style_reference_ids: list[str] = []
    if brand_mode == "preset":
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
            if asset is None or asset.get("kind") != "brand_reference":
                raise HTTPException(status_code=422, detail="风格参考图不存在或类型不正确")

    platform_config: dict[str, Any] | None = None
    if payload.platform_id:
        try:
            platform_config = platform_presets.build_task_config(
                payload.platform_id,
                payload.placement_id,
                payload.merchant_info.model_dump() if payload.merchant_info else None,
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
    task_id = f"task_{uuid.uuid4().hex}"
    now = utcnow()
    doc = {
        "_id": task_id,
        "status": "queued",
        "stage": "pending",
        "progress": 0,
        "input": {
            "product_asset_ids": product_ids,
            "brand_id": brand.id if brand else None,
            "brand_mode": brand_mode,
            "style_reference_asset_ids": style_reference_ids,
            "candidate_count": payload.candidate_count,
            "candidate_batch_id": payload.candidate_batch_id,
            "candidate_index": payload.candidate_index,
            "prompt": payload.prompt,
            "ratio": ratio,
            "generation_mode": payload.generation_mode,
            "platform_id": payload.platform_id,
            "placement_id": (platform_config or {}).get("placement", {}).get("id"),
            "merchant_info": (platform_config or {}).get("merchant_info"),
        },
        "platform": (platform_config or {}).get("platform"),
        "placement": (platform_config or {}).get("placement"),
        "brand": brand_snapshot,
        "canvas": {"width": width, "height": height},
        "provider": {
            "name": "core-ai",
            "model": settings.core_ai_image_model,
            "task_id": None,
            "status": None,
            "progress": 0,
        },
        "brief_snapshot": {"raw": payload.prompt, "brand_id": brand.id if brand else None, "brand_mode": brand_mode, "ratio": ratio, "generation_mode": payload.generation_mode, "platform_id": payload.platform_id, "placement_id": (platform_config or {}).get("placement", {}).get("id")},
        "creative_plan": None,
        "design": None,
        "current_version": 0,
        "messages": [
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "role": "user",
                "content": payload.prompt or "生成一张餐饮促销海报",
                "created_at": now,
                "version": None,
                "actions": [],
                "status": "done",
            }
        ],
        "edit": {"status": "idle", "instruction": None, "error": None},
        "background_asset_id": None,
        "background_clean_asset_id": None,
        "output_asset_id": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
    }
    db.tasks.insert_one(doc)
    background_tasks.add_task(
        run_task,
        db,
        settings,
        request.app.state.store,
        request.app.state.core_ai,
        task_id,
    )
    return task_out(doc)


@router.get("/batches/{batch_id}")
def get_task_batch(batch_id: str, request: Request) -> list[dict[str, Any]]:
    docs = list(
        request.app.state.db.tasks.find({"input.candidate_batch_id": batch_id}).sort(
            "input.candidate_index", 1
        )
    )
    if not docs:
        raise HTTPException(status_code=404, detail="候选批次不存在")
    return [task_out(doc) for doc in docs]


@router.get("/{task_id}")
def get_task(task_id: str, request: Request) -> dict[str, Any]:
    db = request.app.state.db
    return task_out(_get_task_or_404(db, task_id))


@router.post("/{task_id}/edits", status_code=202)
def create_edit(
    task_id: str,
    payload: EditRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    db = request.app.state.db
    doc = _get_task_or_404(db, task_id)
    if doc.get("status") != "succeeded" or not doc.get("design"):
        raise HTTPException(status_code=409, detail="任务尚未生成可编辑的设计")
    if _edit_state(doc)["status"] == "running":
        raise HTTPException(status_code=409, detail="已有修改正在执行，请稍候")
    claimed = db.tasks.find_one_and_update(
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
                    "error": None,
                },
                "stage": "editing",
                "updated_at": utcnow(),
            }
        },
        return_document=True,
    )
    if claimed is None:
        raise HTTPException(status_code=409, detail="已有修改正在执行，请稍候")
    _append_message(
        db,
        task_id,
        role="user",
        content=payload.instruction,
        version=int(doc.get("current_version") or 0) + 1,
    )
    background_tasks.add_task(
        run_edit,
        db,
        request.app.state.settings,
        request.app.state.store,
        request.app.state.core_ai,
        task_id,
        payload.instruction,
    )
    return task_out(_get_task_or_404(db, task_id))


@router.patch("/{task_id}/layers/{layer_id}", status_code=202)
def patch_layer(
    task_id: str,
    layer_id: str,
    payload: LayerPatch,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    db = request.app.state.db
    doc = _get_task_or_404(db, task_id)
    if doc.get("status") != "succeeded" or not isinstance(doc.get("design"), dict):
        raise HTTPException(status_code=409, detail="任务尚未生成可编辑的设计")
    if _edit_state(doc)["status"] == "running":
        raise HTTPException(status_code=409, detail="已有修改正在执行，请稍候")
    patch = payload.to_patch()
    try:
        layer = design_utils.find_layer(doc["design"], layer_id)
        design_utils.apply_layer_patch(layer, patch)
    except design_utils.DesignError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc
    claimed = db.tasks.find_one_and_update(
        {
            "_id": task_id,
            "status": "succeeded",
            "edit.status": {"$ne": "running"},
        },
        {
            "$set": {
                "edit": {
                    "status": "running",
                    "instruction": f"直接编辑图层 {layer_id}",
                    "error": None,
                },
                "stage": "editing",
                "updated_at": utcnow(),
            }
        },
        return_document=True,
    )
    if claimed is None:
        raise HTTPException(status_code=409, detail="已有修改正在执行，请稍候")
    _append_message(
        db,
        task_id,
        role="user",
        content=f"直接调整「{layer.get('name')}」",
        version=int(doc.get("current_version") or 0) + 1,
    )
    background_tasks.add_task(
        run_layer_update,
        db,
        request.app.state.settings,
        request.app.state.store,
        request.app.state.core_ai,
        task_id,
        layer_id,
        patch,
    )
    return task_out(_get_task_or_404(db, task_id))


@router.post("/{task_id}/components/{component_id}/regenerate", status_code=202)
def regenerate_component(
    task_id: str,
    component_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    db = request.app.state.db
    doc = _get_task_or_404(db, task_id)
    if doc.get("status") != "succeeded" or not doc.get("design"):
        raise HTTPException(status_code=409, detail="任务尚未生成可编辑的设计")
    if _edit_state(doc)["status"] == "running":
        raise HTTPException(status_code=409, detail="已有修改正在执行，请稍候")
    design = design_utils.upgrade_design(doc["design"])
    try:
        layer = design_utils.find_layer(design, component_id)
    except design_utils.DesignError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    if layer.get("type") != "visual_component":
        raise HTTPException(status_code=422, detail="目标图层不是可重生成的视觉组件")
    claimed = db.tasks.find_one_and_update(
        {"_id": task_id, "status": "succeeded", "edit.status": {"$ne": "running"}},
        {"$set": {
            "edit": {"status": "running", "instruction": f"重生成组件 {component_id}", "error": None},
            "stage": "regenerating_component",
            "updated_at": utcnow(),
        }},
        return_document=True,
    )
    if claimed is None:
        raise HTTPException(status_code=409, detail="已有修改正在执行，请稍候")
    background_tasks.add_task(
        run_component_regeneration,
        db,
        request.app.state.settings,
        request.app.state.store,
        request.app.state.core_ai,
        task_id,
        component_id,
    )
    return task_out(_get_task_or_404(db, task_id))


@router.get("/{task_id}/versions")
def list_versions(task_id: str, request: Request) -> list[dict[str, Any]]:
    db = request.app.state.db
    _get_task_or_404(db, task_id)
    docs = db.versions.find({"task_id": task_id}).sort("version", 1).limit(100)
    return [_version_out(doc) for doc in docs]


@router.post("/{task_id}/versions/{version}/restore")
def restore_task_version(
    task_id: str,
    version: int,
    request: Request,
) -> dict[str, Any]:
    db = request.app.state.db
    doc = _get_task_or_404(db, task_id)
    if doc.get("status") != "succeeded":
        raise HTTPException(status_code=409, detail="任务尚未生成可恢复的版本")
    if _edit_state(doc)["status"] == "running":
        raise HTTPException(status_code=409, detail="已有修改正在执行，请稍候")
    claimed = db.tasks.find_one_and_update(
        {
            "_id": task_id,
            "status": "succeeded",
            "edit.status": {"$ne": "running"},
        },
        {
            "$set": {
                "edit": {
                    "status": "running",
                    "instruction": f"恢复到 v{version}",
                    "error": None,
                },
                "updated_at": utcnow(),
            }
        },
        return_document=True,
    )
    if claimed is None:
        raise HTTPException(status_code=409, detail="已有修改正在执行，请稍候")
    try:
        restore_version(
            db,
            request.app.state.settings,
            request.app.state.store,
            task_id,
            version,
        )
    except design_utils.DesignError as exc:
        db.tasks.update_one(
            {"_id": task_id},
            {"$set": {"edit.status": "failed", "edit.error": exc.message}},
        )
        raise HTTPException(status_code=422, detail=exc.message) from exc
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "message", str(exc))
        status_code = 404 if getattr(exc, "code", "") == "version_missing" else 409
        db.tasks.update_one(
            {"_id": task_id},
            {"$set": {"edit.status": "failed", "edit.error": code}},
        )
        raise HTTPException(status_code=status_code, detail=code) from exc
    return task_out(_get_task_or_404(db, task_id))


@router.post("/{task_id}/resume", status_code=202)
def resume_task(
    task_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    db = request.app.state.db
    allowed, reason = can_resume(db, task_id)
    if not allowed:
        raise HTTPException(status_code=409, detail=reason)
    db.tasks.update_one(
        {"_id": task_id, "status": {"$in": ["waiting", "interrupted"]}},
        {
            "$set": {
                "status": "queued",
                "error": None,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )
    background_tasks.add_task(
        run_task,
        db,
        request.app.state.settings,
        request.app.state.store,
        request.app.state.core_ai,
        task_id,
    )
    doc = db.tasks.find_one({"_id": task_id})
    if doc is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task_out(doc)


@router.get("/{task_id}/download")
def download_task(
    task_id: str,
    request: Request,
    version: int | None = None,
) -> FileResponse:
    db = request.app.state.db
    doc = _get_task_or_404(db, task_id)
    output_id = doc.get("output_asset_id")
    suffix = ""
    if version is not None:
        snapshot = db.versions.find_one({"task_id": task_id, "version": version})
        if snapshot is None:
            raise HTTPException(status_code=404, detail="历史版本不存在")
        output_id = snapshot.get("output_asset_id")
        suffix = f"-v{version}"
    if not output_id:
        raise HTTPException(status_code=409, detail="任务尚未生成可下载的成品")
    settings = request.app.state.settings
    store = request.app.state.store
    asset = db.assets.find_one({"_id": output_id})
    if asset is None:
        raise HTTPException(status_code=404, detail="成品不存在")
    filename = f"poster-{task_id}{suffix}.png"
    local_path = store.local_path(asset["path"])
    if local_path is not None:
        return FileResponse(
            local_path,
            media_type="image/png",
            filename=filename,
            content_disposition_type="attachment",
        )
    url = store.presign(
        asset["path"],
        expires=settings.cos_presign_expiry_seconds,
        download_name=filename,
    )
    return RedirectResponse(url, status_code=302)
