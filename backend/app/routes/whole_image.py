from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse

from app.schemas import WholeImageEditRequest, WholeImageTaskCreate
from app.services import brand_rules, brand_style, platform_presets, whole_image
from app.services.canvas import canvas_size

router = APIRouter(prefix="/api/whole-image", tags=["whole-image"])


def _edit_state(doc: dict[str, Any]) -> dict[str, Any]:
    edit = doc.get("edit")
    if not isinstance(edit, dict):
        return {
            "status": "idle",
            "instruction": None,
            "source_version_id": None,
            "error": None,
        }
    return {
        "status": edit.get("status") or "idle",
        "instruction": edit.get("instruction"),
        "source_version_id": edit.get("source_version_id"),
        "error": edit.get("error"),
    }


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
        images.append(
            {
                "asset_id": asset_id,
                "name": str(image.get("name") or "参考图片"),
                "kind": str(image.get("kind") or "reference"),
                "preview_url": f"/api/assets/{asset_id}/file" if asset else None,
            }
        )
    return {
        "endpoint": str(snapshot.get("endpoint") or ""),
        "model": str(snapshot.get("model") or ""),
        "prompt": str(snapshot.get("prompt") or ""),
        "parameters": snapshot.get("parameters") or {},
        "images": images,
    }


def task_out(db: Any, doc: dict[str, Any]) -> dict[str, Any]:
    version_doc = _version_for_task(db, str(doc["_id"]))
    model_input = doc.get("model_input_attempt") or doc.get("model_input_latest")
    if model_input is None:
        latest_version = db.whole_versions.find_one(
            {"task_id": str(doc["_id"]), "model_input": {"$exists": True}},
            sort=[("version", -1)],
        )
        model_input = (latest_version or {}).get("model_input")
    output_id = (
        version_doc.get("output_asset_id") if version_doc else doc.get("output_asset_id")
    )
    version_number = int(
        (version_doc or {}).get("version") or doc.get("current_version") or 0
    )
    version_id = str(version_doc["_id"]) if version_doc else None
    canvas = doc.get("canvas") or {"width": 0, "height": 0}
    if version_doc:
        canvas = {
            "width": int(version_doc.get("width") or canvas.get("width") or 0),
            "height": int(version_doc.get("height") or canvas.get("height") or 0),
        }
    provider = doc.get("provider") or {}
    preview_url = (
        f"/api/assets/{output_id}/file?v={version_number}" if output_id else None
    )
    download_url = (
        f"/api/whole-image/tasks/{doc['_id']}/download?version_id={version_id}"
        if output_id and version_id
        else None
    )
    return {
        "id": doc["_id"],
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
        "model_input": _model_input_out(db, model_input),
        "selected_version_id": version_id,
        "current_version_id": version_id,
        "current_version": version_number,
        "preview_url": preview_url,
        "download_url": download_url,
        "messages": doc.get("messages") or [],
        "edit": _edit_state(doc),
        "error": doc.get("error"),
        "created_at": doc["created_at"],
        "updated_at": doc["updated_at"],
    }


def _version_out(db: Any, doc: dict[str, Any]) -> dict[str, Any]:
    task_id = str(doc["task_id"])
    version_id = str(doc["_id"])
    version = int(doc.get("version") or 0)
    output_id = doc.get("output_asset_id")
    asset = db.assets.find_one({"_id": output_id}) if output_id else None
    operation = doc.get("operation") or ("generate" if version <= 1 else "edit")
    return {
        "id": version_id,
        "version": version,
        "parent_version_id": doc.get("parent_version_id"),
        "operation": operation,
        "instruction": doc.get("instruction") or "",
        "width": int(doc.get("width") or (asset or {}).get("width") or 0),
        "height": int(doc.get("height") or (asset or {}).get("height") or 0),
        "output_asset_id": output_id,
        "preview_url": (
            f"/api/assets/{output_id}/file?v={version}" if output_id else None
        ),
        "download_url": (
            f"/api/whole-image/tasks/{task_id}/download?version_id={version_id}"
            if output_id
            else None
        ),
        "created_at": doc["created_at"],
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
        if product is None or product.get("kind") not in {"product", "logo"}:
            raise HTTPException(status_code=422, detail="素材图不存在或类型不正确")
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
            if asset is None or asset.get("kind") != "brand_reference":
                raise HTTPException(status_code=422, detail="风格参考图不存在或类型不正确")

    logo_asset_id = payload.logo_asset_id
    if logo_asset_id:
        logo_asset = db.assets.find_one({"_id": logo_asset_id})
        if logo_asset is None or logo_asset.get("kind") != "logo":
            raise HTTPException(status_code=422, detail="Logo 不存在或类型不正确")
        if brand is not None and logo_asset_id not in brand.logo_asset_ids:
            raise HTTPException(status_code=422, detail="所选 Logo 不属于当前品牌风格")

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


@router.get("/tasks/{task_id}/download")
def download_task(
    task_id: str,
    request: Request,
    version_id: str | None = None,
) -> Response:
    db = request.app.state.db
    _get_task_or_404(db, task_id)
    selected = _version_for_task(db, task_id, version_id=version_id)
    output_id = selected.get("output_asset_id") if selected else None
    if not output_id:
        raise HTTPException(status_code=409, detail="任务尚未生成可下载的成品")
    settings = request.app.state.settings
    store = request.app.state.store
    asset = db.assets.find_one({"_id": output_id})
    if asset is None:
        raise HTTPException(status_code=404, detail="成品不存在")
    number = int((selected or {}).get("version") or 0)
    filename = f"whole-image-{task_id}-v{number}.png"
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
