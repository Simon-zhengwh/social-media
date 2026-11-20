from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

from app.request_context import request_db, request_merchant_id, request_store
from app.routes import whole_image as whole_routes
from app.schemas import PosterSeriesRunCreate, SeriesHtmlSaveRequest, SeriesInteriorsRequest
from app.services import poster_series, poster_series_round
from app.services.poster_series import SeriesError

router = APIRouter(prefix="/whole-image", tags=["poster-series"])


def _series_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except SeriesError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except whole_routes.HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - surface service validation as a 409/422
        message = getattr(exc, "message", None) or str(exc) or "系列图请求失败"
        status = int(getattr(exc, "status_code", 409) or 409)
        raise HTTPException(status_code=status, detail=message) from exc


@router.get("/series-config")
def series_config(request: Request) -> dict[str, Any]:
    return {"enabled": poster_series.enabled(request.app.state.settings)}


@router.get("/workspaces/{workspace_id}/mode")
def workspace_mode(workspace_id: str, request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    return _series_call(
        poster_series.get_workspace_mode, request_db(request), workspace_id, settings
    )


@router.get("/workspaces/{workspace_id}/series-groups")
def list_series_groups(workspace_id: str, request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    _series_call(poster_series.require_enabled, settings)
    return _series_call(
        poster_series.get_workspace_series,
        request_db(request), workspace_id, settings,
        merchant_id=request_merchant_id(request),
    )


@router.post("/workspaces/{workspace_id}/series-runs", status_code=202)
def create_series_run(
    workspace_id: str,
    payload: PosterSeriesRunCreate,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    settings = request.app.state.settings
    _series_call(poster_series.require_enabled, settings)
    db = request_db(request)
    deps = (db, settings, request_store(request), request.app.state.core_ai)

    def _build_task(page_payload):
        return whole_routes.build_task_document(page_payload, request)

    return _series_call(
        poster_series_round.create_run,
        db, settings, workspace_id, payload,
        build_task=_build_task,
        launch=lambda run_id: background_tasks.add_task(poster_series_round.run_generation, *deps, run_id),
        merchant_id=request_merchant_id(request),
    )


@router.post("/series-groups/{group_id}/interiors", status_code=202)
def start_series_interiors(
    group_id: str,
    payload: SeriesInteriorsRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    settings = request.app.state.settings
    _series_call(poster_series.require_enabled, settings)
    db = request_db(request)
    deps = (db, settings, request_store(request), request.app.state.core_ai)
    return _series_call(
        poster_series.start_interiors,
        db, settings, group_id,
        cover_version_id=payload.cover_version_id,
        cover_output_id=payload.cover_output_id,
        launch=lambda task_id: background_tasks.add_task(
            poster_series.run_page_generation, *deps, task_id
        ),
        merchant_id=request_merchant_id(request),
    )


@router.post("/series-groups/{group_id}/pages/{page_id}/retry", status_code=202)
def retry_series_page(
    group_id: str,
    page_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    settings = request.app.state.settings
    _series_call(poster_series.require_enabled, settings)
    db = request_db(request)
    deps = (db, settings, request_store(request), request.app.state.core_ai)
    return _series_call(
        poster_series.retry_page,
        db, settings, group_id, page_id,
        launch=lambda task_id: background_tasks.add_task(
            poster_series.run_page_generation, *deps, task_id
        ),
        merchant_id=request_merchant_id(request),
    )


@router.post("/series-groups/{group_id}/pages/{page_id}/html-saves")
def save_series_html_page(
    group_id: str,
    page_id: str,
    payload: SeriesHtmlSaveRequest,
    request: Request,
) -> dict[str, Any]:
    settings = request.app.state.settings
    _series_call(poster_series.require_enabled, settings)
    return _series_call(
        poster_series.html_save,
        request_db(request), settings, request_store(request),
        group_id, page_id, payload,
        merchant_id=request_merchant_id(request),
    )


@router.get("/series-groups/{group_id}/pages/{page_id}/download")
def download_series_page(group_id: str, page_id: str, request: Request) -> Response:
    settings = request.app.state.settings
    _series_call(poster_series.require_enabled, settings)
    data, filename = _series_call(
        poster_series.page_download, request_db(request), request_store(request),
        group_id, page_id,
    )
    return Response(
        content=data,
        media_type="image/png",
        headers={
            "Content-Disposition": whole_routes._artifact_content_disposition(filename),
            "Content-Type": "image/png",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/series-groups/{group_id}/download")
def download_series_group(group_id: str, request: Request) -> Response:
    settings = request.app.state.settings
    _series_call(poster_series.require_enabled, settings)
    data, filename = _series_call(
        poster_series.group_download, request_db(request), request_store(request), group_id
    )
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": whole_routes._artifact_content_disposition(filename),
            "Content-Type": "application/zip",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
