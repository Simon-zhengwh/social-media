from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from app.api_errors import am_http_error
from app.middleware import is_local_bypass as _local_bypass
from app.request_context import request_db
from app.services import am_assets, merchant_profile
from app.services.am_client import AMClientError

router = APIRouter(tags=["merchant-context"])

# Mounted without the /merchants/{merchant_id} prefix: picking a merchant cannot require one.
merchant_list_router = APIRouter(prefix="/api/merchants", tags=["merchants"])


@merchant_list_router.get("")
def list_merchants(request: Request, keyword: str = "") -> dict[str, Any]:
    """The merchants the operator may switch to.

    Same list the AM Console launcher shows, read with the caller's own session. It is
    deliberately not scoped to one merchant: it exists to choose one.
    """
    if _local_bypass(request):
        # No console session locally: the configured merchant is the only one on offer.
        merchant_id = request.app.state.settings.am_bypass_merchant_id.strip()
        merchants = [{"id": merchant_id, "name": "本地商户"}] if merchant_id else []
        return {"total": len(merchants), "merchants": merchants}
    am = request.app.state.am
    try:
        payload = am.search_merchants(request.headers.get("cookie", ""), keyword=keyword)
    except AMClientError as exc:
        raise am_http_error(exc) from exc
    merchants = [
        {"id": str(item.get("merchant_id") or ""), "name": str(item.get("name") or "")}
        for item in payload.get("merchants") or []
        if item.get("merchant_id")
    ]
    return {"total": int(payload.get("total") or len(merchants)), "merchants": merchants}


@router.get("/context")
def get_merchant_context(merchant_id: str, request: Request) -> dict[str, Any]:
    bypass = _local_bypass(request)
    if bypass:
        projected = merchant_profile.stored_profile(request.app.state.raw_db, merchant_id)
    else:
        try:
            profile = merchant_profile.load_profile(request.app.state.am, request.state.am_cookie, merchant_id)
        except AMClientError as exc:
            raise am_http_error(exc) from exc
        projected = merchant_profile.project_profile(profile)
        request.state.merchant_profile = projected
    return {
        "merchant": request.state.merchant_context,
        "profile": projected,
        "features": {
            "am_media_library": not bypass,
            "maintenance_cli": True,
            # Agent benchmarks only make sense against the local model/session wiring.
            "agent_tests": bypass,
            # SMB material filtering reads this machine's folders, so it is local-only.
            "material_filters": bypass,
        },
    }


@router.get("/am-assets")
def list_am_assets(
    merchant_id: str,
    request: Request,
    keyword: str = "",
    tag: str = "",
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    if _local_bypass(request):
        # The AM media library needs a console session; local dev only has uploads.
        return am_assets.list_local(
            request_db(request),
            merchant_id,
            keyword=keyword,
            tag=tag,
            skip=skip,
            limit=limit,
        )
    try:
        return am_assets.list_for_merchant(
            request_db(request),
            request.app.state.am,
            request.state.am_cookie,
            merchant_id,
            keyword=keyword,
            tag=tag,
            skip=skip,
            limit=limit,
        )
    except AMClientError as exc:
        raise am_http_error(exc) from exc

