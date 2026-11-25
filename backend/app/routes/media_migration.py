"""Local-only endpoints that copy a merchant's AM Console media library to Prod.

The routes live outside ``/api/merchants/{merchant_id}`` on purpose: the operator
enters both merchant ids by hand, so neither has to exist in this workspace's
imported catalog. Every route is gated on the local AM bypass, and every write
validates the same CSRF headers the merchant middleware expects.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.middleware import LOCAL_ORIGINS, WRITE_METHODS, is_local_bypass
from app.services import media_migration as service
from app.services.media_migration import MediaMigrationError

DISABLED_MESSAGE = "素材迁移仅在本地开发模式开放"


class MigrationPayload(BaseModel):
    uat_base_url: str = ""
    uat_merchant_id: str = ""
    # Sessions travel in the body of this one request only; nothing persists them.
    uat_cookie: str = ""
    prod_base_url: str = ""
    prod_merchant_id: str = ""
    prod_cookie: str = ""
    skip_same_name: bool = True
    force: bool = False
    limit: int = Field(default=0, ge=0)


def guard(request: Request) -> None:
    """Reject anything a deployed site could reach, before the body is even read."""
    if not is_local_bypass(request):
        raise MediaMigrationError("media_migration_disabled", DISABLED_MESSAGE, 404)
    if request.method.upper() not in WRITE_METHODS:
        return
    settings = request.app.state.settings
    origin = request.headers.get("origin")
    local_origin = str(settings.social_media_origin or "").rstrip("/")
    if origin not in LOCAL_ORIGINS and origin != local_origin:
        raise MediaMigrationError("csrf_origin_rejected", "写请求来源不受信任", 403)
    if request.headers.get("x-media-request") != "1":
        raise MediaMigrationError("csrf_header_required", "写请求缺少 X-Media-Request 头", 403)


router = APIRouter(
    prefix="/api/media-migration",
    tags=["media-migration"],
    dependencies=[Depends(guard)],
)


def _transport(request: Request) -> Any:
    # Test seam: cases inject an httpx.MockTransport instead of reaching the network.
    return getattr(request.app.state, "media_migration_transport", None)


def _settings(request: Request):
    return request.app.state.settings


def _db(request: Request):
    # Jobs and checkpoints live in the local workspace database, not in a merchant scope.
    return request.app.state.raw_db


@router.get("/defaults")
def get_defaults(request: Request) -> dict[str, str]:
    return service.defaults(_settings(request))


@router.post("/preview")
def preview_migration(payload: MigrationPayload, request: Request) -> dict[str, Any]:
    return service.preview(
        _settings(request), _db(request), payload.model_dump(), transport=_transport(request)
    )


@router.get("/jobs")
def list_migration_jobs(
    request: Request,
    uat_merchant_id: str = "",
    prod_merchant_id: str = "",
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    return service.list_jobs(
        _db(request),
        uat_merchant_id=uat_merchant_id,
        prod_merchant_id=prod_merchant_id,
        limit=limit,
    )


@router.post("/jobs")
def start_migration_job(payload: MigrationPayload, request: Request) -> dict[str, Any]:
    return service.start_job(
        _settings(request), _db(request), payload.model_dump(), transport=_transport(request)
    )


@router.get("/jobs/{job_id}")
def get_migration_job(job_id: str, request: Request) -> dict[str, Any]:
    return service.get_job(_db(request), job_id)


@router.post("/jobs/{job_id}/cancel")
def cancel_migration_job(job_id: str, request: Request) -> dict[str, Any]:
    return service.cancel_job(_db(request), job_id)
