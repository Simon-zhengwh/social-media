from __future__ import annotations

import re
from urllib.parse import unquote

from fastapi import Request
from fastapi.responses import JSONResponse

from app.services import maintenance, merchant_profile
from app.services.am_client import AMClientError
from app.services.media_object_store import create_merchant_store
from app.services.scoped_db import ScopedDatabase

MERCHANT_PATH = re.compile(r"^/api/merchants/([^/]+)(?:/|$)")
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


async def merchant_context_middleware(request: Request, call_next):
    match = MERCHANT_PATH.match(request.url.path)
    if not match:
        return await call_next(request)

    merchant_id = unquote(match.group(1))
    cookie = request.headers.get("cookie", "").strip()
    try:
        access = request.app.state.am.access(cookie, merchant_id)
    except AMClientError as exc:
        return _error(exc.status_code, exc.code, exc.message)
    request.state.merchant_id = merchant_id
    request.state.merchant_access = access
    request.state.am_cookie = cookie
    request.state.merchant_context = {
        "user_id": access.get("user_id"),
        "user_name": access.get("user_name"),
        "merchant_id": access.get("merchant_id") or merchant_id,
        "merchant_name": access.get("merchant_name"),
    }
    needs_generation_profile = request.method.upper() == "POST" and request.url.path.endswith(
        ("/prompts/optimize", "/whole-image/tasks")
    )
    if "/brands" in request.url.path or needs_generation_profile:
        try:
            request.state.merchant_profile = merchant_profile.project_profile(
                merchant_profile.load_profile(request.app.state.am, cookie, merchant_id)
            )
        except AMClientError as exc:
            return _error(exc.status_code if exc.status_code in {401, 403, 404} else 502, exc.code, exc.message)
    else:
        request.state.merchant_profile = None
    request.state.db = ScopedDatabase(request.app.state.raw_db, merchant_id)
    request.state.store = create_merchant_store(
        request.app.state.settings,
        request.state.db,
        request.app.state.am,
        merchant_id,
    )

    if request.method.upper() in WRITE_METHODS:
        origin = request.headers.get("origin")
        if origin != request.app.state.settings.social_media_origin:
            return _error(403, "csrf_origin_rejected", "写请求来源不受信任")
        if request.headers.get("x-media-request") != "1":
            return _error(403, "csrf_header_required", "写请求缺少 X-Media-Request 头")
        try:
            with maintenance.admit(request.app.state.raw_db):
                return await call_next(request)
        except maintenance.MaintenanceError as exc:
            return _error(503, "maintenance_enabled", str(exc))
    return await call_next(request)


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}})
