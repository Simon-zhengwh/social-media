from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse

from app.request_context import request_db, request_store
from app.schemas import BackgroundReplacementRequest
from app.services import background_replace as service, request_assets

router = APIRouter(prefix="/background-replacements", tags=["background-replace"])


def _configured(request: Request) -> None:
    if not request.app.state.settings.core_ai_api_key.strip():
        raise HTTPException(status_code=503, detail="未配置 CORE_AI_API_KEY")


def _schedule(request: Request, background_tasks: BackgroundTasks, task_id: str, *, refresh: bool = False) -> None:
    background_tasks.add_task(
        service.run_task,
        request_db(request),
        request.app.state.settings,
        request_store(request),
        request.app.state.core_ai,
        task_id,
        refresh=refresh,
    )


@router.post("", status_code=202)
def create_replacement(payload: BackgroundReplacementRequest, request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    _configured(request)
    data = payload.model_dump()
    data["source_asset_id"] = request_assets.resolve_asset_id(request, data["source_asset_id"])
    data["replacements"] = [
        {**item, "asset_id": request_assets.resolve_asset_id(request, item["asset_id"])}
        for item in data["replacements"]
    ]
    try:
        doc = service.create_task(request_db(request), **data)
    except service.BackgroundReplaceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    _schedule(request, background_tasks, doc["_id"])
    return service.task_out(doc)


@router.get("/{task_id}")
def get_replacement(task_id: str, request: Request) -> dict[str, Any]:
    try:
        return service.task_out(service.get_task(request_db(request), task_id))
    except service.BackgroundReplaceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/{task_id}/refresh-result", status_code=202)
def refresh_replacement(task_id: str, request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    _configured(request)
    try:
        doc = service.queue_refresh(request_db(request), task_id)
    except service.BackgroundReplaceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    if doc["status"] == "queued":
        _schedule(request, background_tasks, task_id, refresh=True)
    return service.task_out(doc)


@router.get("/{task_id}/download")
def download_replacement(task_id: str, request: Request) -> Response:
    state = request.app.state
    store = request_store(request)
    try:
        task = service.get_task(request_db(request), task_id)
    except service.BackgroundReplaceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    output = task.get("output")
    if task["status"] != "succeeded" or not output:
        raise HTTPException(status_code=404, detail="最终结果尚未就绪")
    key = output["path"]
    name = f"background-replace-{task_id}.png"
    local = store.local_path(key)
    if local is not None:
        return FileResponse(local, media_type="image/png", filename=name)
    return RedirectResponse(store.presign(key, expires=state.settings.cos_presign_expiry_seconds, download_name=name), status_code=302)
