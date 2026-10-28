from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse

from app.schemas import PosterEditBatchRequest, PosterEditRequest
from app.services import poster_edit as service

router = APIRouter(prefix="/api/poster-edits", tags=["poster-edit"])


def _require_configured(request: Request) -> None:
    if not request.app.state.settings.core_ai_api_key.strip():
        raise HTTPException(status_code=503, detail="未配置 CORE_AI_API_KEY")


@router.post("", status_code=202)
def create_poster_edit(
    payload: PosterEditRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    _require_configured(request)
    try:
        task = service.create_task(
            request.app.state.db,
            prompt=payload.prompt,
            asset_ids=payload.asset_ids,
        )
    except service.PosterEditError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    background_tasks.add_task(
        service.run_task,
        request.app.state.db,
        request.app.state.settings,
        request.app.state.store,
        request.app.state.core_ai,
        task["_id"],
    )
    return service.task_out(request.app.state.db, task)


@router.post("/batches", status_code=202)
def create_poster_edit_batch(
    payload: PosterEditBatchRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    _require_configured(request)
    try:
        batch_id, _ = service.create_batch(
            request.app.state.db,
            prompt=payload.prompt,
            asset_ids=payload.asset_ids,
            candidate_count=payload.candidate_count,
        )
    except service.PosterEditError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    background_tasks.add_task(
        service.run_batch,
        request.app.state.db,
        request.app.state.settings,
        request.app.state.store,
        request.app.state.core_ai,
        batch_id,
    )
    return service.batch_out(request.app.state.db, batch_id)


@router.get("/batches/{batch_id}")
def get_poster_edit_batch(batch_id: str, request: Request) -> dict[str, Any]:
    try:
        return service.batch_out(request.app.state.db, batch_id)
    except service.PosterEditError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/{task_id}")
def get_poster_edit(task_id: str, request: Request) -> dict[str, Any]:
    task = request.app.state.db.poster_edit_tasks.find_one({"_id": task_id})
    if task is None:
        raise HTTPException(status_code=404, detail="修改任务不存在")
    return service.task_out(request.app.state.db, task)


@router.get("/{task_id}/download")
def download_poster_edit(
    task_id: str,
    output_id: str,
    request: Request,
) -> Response:
    try:
        local_path, filename, url = service.download_output(
            request.app.state.db,
            request.app.state.store,
            request.app.state.settings,
            task_id,
            output_id,
        )
    except service.PosterEditError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    if local_path is not None:
        return FileResponse(
            local_path,
            media_type="image/png",
            filename=filename,
            content_disposition_type="attachment",
        )
    assert url is not None
    return RedirectResponse(url, status_code=302)
