from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.services.sample_assets import import_samples

router = APIRouter(prefix="/api", tags=["samples"])


@router.get("/samples")
def get_samples(request: Request) -> dict[str, Any]:
    return import_samples(
        request.app.state.db,
        request.app.state.settings,
        request.app.state.store,
    )
