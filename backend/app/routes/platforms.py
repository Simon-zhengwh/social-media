from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.services import platform_presets

router = APIRouter(prefix="/api/platforms", tags=["platforms"])


@router.get("")
def list_platforms() -> list[dict[str, Any]]:
    return platform_presets.catalog()
