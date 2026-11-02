from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.request_context import request_db
from app.services import am_assets, merchant_profile
from app.services.am_client import AMClientError

router = APIRouter(tags=["merchant-context"])


@router.get("/context")
def get_merchant_context(merchant_id: str, request: Request) -> dict[str, Any]:
    try:
        profile = merchant_profile.load_profile(request.app.state.am, request.state.am_cookie, merchant_id)
    except AMClientError as exc:
        raise _am_http_error(exc) from exc
    projected = merchant_profile.project_profile(profile)
    request.state.merchant_profile = projected
    return {
        "merchant": request.state.merchant_context,
        "profile": projected,
        "features": {"am_media_library": True, "maintenance_cli": True},
    }


@router.get("/am-assets")
def list_am_assets(
    merchant_id: str,
    request: Request,
    keyword: str = "",
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    try:
        return am_assets.list_for_merchant(
            request_db(request),
            request.app.state.am,
            request.state.am_cookie,
            merchant_id,
            keyword=keyword,
            skip=skip,
            limit=limit,
        )
    except AMClientError as exc:
        raise _am_http_error(exc) from exc


def _am_http_error(exc: AMClientError) -> HTTPException:
    status = exc.status_code if exc.status_code in {401, 403, 404, 413, 503} else 502
    return HTTPException(status_code=status, detail=exc.message)
