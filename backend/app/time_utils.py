from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utc_iso(value: Any) -> Any:
    """Serialize datetimes as UTC ISO-8601 with an explicit Z suffix."""
    if not isinstance(value, datetime):
        return value
    current = value
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    return current.isoformat(timespec="milliseconds").replace("+00:00", "Z")
