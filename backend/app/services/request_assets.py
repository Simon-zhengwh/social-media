from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from app.services import am_assets
from app.services.am_client import AMClientError


def resolve_asset_ids(request: Request, asset_ids: list[str]) -> list[str]:
    resolved = []
    for asset_id in asset_ids:
        resolved.append(resolve_asset_id(request, asset_id))
    return list(dict.fromkeys(resolved))


def resolve_asset_id(request: Request, asset_id: str | None) -> str | None:
    if not asset_id:
        return None
    if not asset_id.startswith("am:"):
        return asset_id
    state = getattr(request, "state", None)
    if state is None or not hasattr(state, "db"):
        return asset_id
    try:
        return am_assets.import_if_needed(
            request.state.db,
            request.state.store,
            request.app.state.settings,
            request.app.state.am,
            request.state.am_cookie,
            request.state.merchant_id,
            asset_id,
        )
    except AMClientError as exc:
        status = exc.status_code if exc.status_code in {401, 403, 404, 413, 503} else 502
        raise HTTPException(status_code=status, detail=exc.message) from exc


def resolve_asset_map(request: Request, values: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    result = dict(values)
    for key in keys:
        value = result.get(key)
        if isinstance(value, list):
            result[key] = resolve_asset_ids(request, [str(item) for item in value])
        elif isinstance(value, str):
            result[key] = resolve_asset_id(request, value)
    return result
