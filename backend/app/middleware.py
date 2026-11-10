from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

from app.services import maintenance, merchant_profile
from app.services.am_client import AMClientError
from app.services.media_object_store import create_merchant_store
from app.services.scoped_db import ScopedDatabase

MERCHANT_PATH = re.compile(r"^/api/merchants/([^/]+)(?:/|$)")
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
LOCAL_ORIGINS = frozenset({"http://127.0.0.1:5173", "http://localhost:5173"})


def _is_loopback_origin(origin: str) -> bool:
    parsed = urlsplit(origin.strip().rstrip("/"))
    return parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def local_bypass_enabled(settings: Any) -> bool:
    """True when this process may run without an AM Console session."""
    return bool(getattr(settings, "am_bypass_local", False)) and _is_loopback_origin(
        str(getattr(settings, "social_media_origin", ""))
    )


def _local_bypass_context(request: Request) -> dict[str, object] | None:
    """Return a stand-in merchant context when AM Console is bypassed locally.

    Enabled only through AM_BYPASS_LOCAL and only when this process serves a loopback
    origin, so a deployed host cannot switch the bypass on with a stray environment
    variable. The merchant id still comes from the URL, so every query stays scoped.
    """
    if not local_bypass_enabled(request.app.state.settings):
        return None
    return {
        "user_id": "local-dev",
        "user_name": "本地开发",
        "merchant_name": "本地商户",
    }


def is_local_bypass(request: Request) -> bool:
    """True when this request runs under the local AM bypass."""
    return local_bypass_enabled(getattr(request.app.state, "settings", None))


async def merchant_context_middleware(request: Request, call_next):
    match = MERCHANT_PATH.match(request.url.path)
    if not match:
        return await call_next(request)

    merchant_id = unquote(match.group(1))
    cookie = request.headers.get("cookie", "").strip()
    bypass = _local_bypass_context(request)
    try:
        if bypass is not None:
            access = {"merchant_id": merchant_id, **bypass}
        else:
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
    wants_profile = "/brands" in request.url.path or needs_generation_profile
    if bypass is not None:
        # No AM session locally: use the profile the import script already stored.
        request.state.merchant_profile = (
            merchant_profile.stored_profile(request.app.state.raw_db, merchant_id)
            if wants_profile else None
        )
    elif wants_profile:
        try:
            request.state.merchant_profile = merchant_profile.project_profile(
                merchant_profile.load_profile(request.app.state.am, cookie, merchant_id)
            )
        except AMClientError as exc:
            status = exc.status_code if exc.status_code in {401, 403, 404} else 502
            return _error(status, exc.code, exc.message)
    else:
        request.state.merchant_profile = None
    request.state.db = ScopedDatabase(request.app.state.raw_db, merchant_id)
    if bypass is not None:
        # Local dev keeps files on disk; UAT routes them through AM media storage.
        request.state.store = request.app.state.local_store
    else:
        request.state.store = create_merchant_store(
            request.app.state.settings,
            request.state.db,
            request.app.state.am,
            merchant_id,
        )

    if request.method.upper() in WRITE_METHODS:
        origin = request.headers.get("origin")
        if bypass is not None:
            local_origin = request.app.state.settings.social_media_origin.rstrip("/")
            allowed_origin = origin in LOCAL_ORIGINS or origin == local_origin
        else:
            allowed_origin = origin == request.app.state.settings.social_media_origin
        if not allowed_origin:
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
