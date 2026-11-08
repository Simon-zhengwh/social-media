from __future__ import annotations

from typing import Any


def request_db(request: Any) -> Any:
    state = getattr(request, "state", None)
    scoped = getattr(state, "db", None) if state is not None else None
    return scoped or request.app.state.db


def request_store(request: Any) -> Any:
    state = getattr(request, "state", None)
    scoped = getattr(state, "store", None) if state is not None else None
    return scoped or request.app.state.store


def request_merchant_id(request: Any) -> str:
    state = getattr(request, "state", None)
    value = getattr(state, "merchant_id", None) if state is not None else None
    if not value:
        path_params = getattr(request, "path_params", None) or {}
        value = path_params.get("merchant_id") if isinstance(path_params, dict) else None
    if not value:
        value = getattr(request.app.state, "merchant_id", None)
    if not value:
        value = "merchant"
    return str(value)


def request_profile(request: Any) -> dict[str, Any] | None:
    state = getattr(request, "state", None)
    return getattr(state, "merchant_profile", None) if state is not None else None
