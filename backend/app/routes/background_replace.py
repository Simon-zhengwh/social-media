from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse

from app.schemas import BackgroundReplacementRequest
from app.services import background_replace as service

router = APIRouter(prefix="/api/background-replacements", tags=["background-replace"])


def _configured(request: Request) -> None:
    if not request.app.state.settings.core_ai_api_key.strip():
        raise HTTPException(status_code=503, detail="未配置 CORE_AI_API_KEY")


def _schedule(request: Request, background_tasks: BackgroundTasks, task_id: str, *, refresh: bool = False) -> None:
    state = request.app.state
    background_tasks.add_task(service.run_task, state.db, state.settings, state.store, state.core_ai, task_id, refresh=refresh)


@router.post("", status_code=202)
def create_replacement(payload: BackgroundReplacementRequest, request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    _configured(request)
    try:
        doc = service.create_task(request.app.state.db, **payload.model_dump())
    except service.BackgroundReplaceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    _schedule(request, background_tasks, doc["_id"])
    return service.task_out(doc)


@router.get("/{task_id}")
def get_replacement(task_id: str, request: Request) -> dict[str, Any]:
    try:
        return service.task_out(service.get_task(request.app.state.db, task_id))
    except service.BackgroundReplaceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/{task_id}/refresh-result", status_code=202)
def refresh_replacement(task_id: str, request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    _configured(request)
    try:
        doc = service.queue_refresh(request.app.state.db, task_id)
    except service.BackgroundReplaceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    if doc["status"] == "queued":
        _schedule(request, background_tasks, task_id, refresh=True)
    return service.task_out(doc)


@router.get("/{task_id}/download")
def download_replacement(task_id: str, request: Request) -> Response:
    state = request.app.state
    try:
        task = service.get_task(state.db, task_id)
    except service.BackgroundReplaceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    output = task.get("output")
    if task["status"] != "succeeded" or not output:
        raise HTTPException(status_code=404, detail="最终结果尚未就绪")
    key = output["path"]
    name = f"background-replace-{task_id}.png"
    local = state.store.local_path(key)
    if local is not None:
        return FileResponse(local, media_type="image/png", filename=name)
    return RedirectResponse(state.store.presign(key, expires=state.settings.cos_presign_expiry_seconds, download_name=name), status_code=302)
