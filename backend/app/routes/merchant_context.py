from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.api_errors import am_http_error
from app.middleware import is_local_bypass as _local_bypass
from app.request_context import request_db
from app.services import am_assets, merchant_profile, poster_finalizations
from app.services.am_client import AMClientError

router = APIRouter(tags=["merchant-context"])

# Mounted without the /merchants/{merchant_id} prefix: picking a merchant cannot require one.
merchant_list_router = APIRouter(prefix="/api/merchants", tags=["merchants"])


def _validate_list_context(request: Request) -> None:
    if _local_bypass(request):
        return
    context_merchant_id = str(request.query_params.get("context_merchant_id") or "").strip()
    if not context_merchant_id:
        raise HTTPException(status_code=400, detail="缺少 AM 商户上下文")
    if request.app.state.raw_db.merchants.find_one({"_id": context_merchant_id}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="当前商户未导入 social-media")
    try:
        request.app.state.am.access(
            request.headers.get("cookie", ""), context_merchant_id
        )
    except AMClientError as exc:
        raise am_http_error(exc) from exc


def _merchant_catalog(raw_db: Any, keyword: str) -> list[dict[str, str]]:
    needle = keyword.strip().casefold()
    merchants = [
        {"id": str(record["_id"]), "name": str(record.get("name") or record["_id"])}
        for record in raw_db.merchants.find({}, {"_id": 1, "name": 1})
        if not needle or needle in str(record.get("name") or record["_id"]).casefold()
    ]
    return sorted(merchants, key=lambda item: (item["name"].casefold(), item["id"]))


@merchant_list_router.get("")
def list_merchants(request: Request, keyword: str = "") -> dict[str, Any]:
    """List every manually imported merchant; AM validates only the current session context."""
    _validate_list_context(request)
    merchants = _merchant_catalog(request.app.state.raw_db, keyword)
    return {"total": len(merchants), "merchants": merchants}


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
        page = am_assets.list_local(
            request_db(request),
            merchant_id,
            keyword=keyword,
            tag=tag,
            skip=skip,
            limit=limit,
        )
        page['items'] = poster_finalizations.enrich_assets(request_db(request), page['items'])
        return page
    try:
        page = am_assets.list_for_merchant(
            request_db(request),
            request.app.state.am,
            request.state.am_cookie,
            merchant_id,
            keyword=keyword,
            tag=tag,
            skip=skip,
            limit=limit,
        )
        page['items'] = poster_finalizations.enrich_assets(request_db(request), page['items'])
        return page
    except AMClientError as exc:
        raise am_http_error(exc) from exc
