from __future__ import annotations

from fastapi import HTTPException

from app.services.am_client import AMClientError


def am_http_error(exc: AMClientError) -> HTTPException:
    """Map AM failures onto the status codes the browser already distinguishes.

    404/403 mean the merchant or the resource is gone; anything else is treated as an upstream
    problem so the client never clears a valid merchant cache because of a transient failure.
    """
    status = exc.status_code if exc.status_code in {401, 403, 404, 413, 503} else 502
    return HTTPException(status_code=status, detail=exc.message)
