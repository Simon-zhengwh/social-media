"""Merchant catalog for the local AM bypass.

There is no console session locally, so the switchable merchants come from
configuration: the primary ``AM_BYPASS_MERCHANT_ID`` first, then every entry of
``AM_BYPASS_MERCHANTS`` written as ``<merchant_id>=<display name>`` and
separated by ``;`` (newlines work too).
"""
from __future__ import annotations

from typing import Any

DEFAULT_MERCHANT_NAME = "本地商户"
SEPARATORS = (";", "\n")


def parse_merchant_entries(raw: str) -> list[dict[str, str]]:
    """Parse ``id=name;id=name`` into ``{"id", "name"}`` entries, keeping order."""
    entries: list[dict[str, str]] = []
    normalized = raw
    for separator in SEPARATORS[1:]:
        normalized = normalized.replace(separator, SEPARATORS[0])
    for chunk in normalized.split(SEPARATORS[0]):
        chunk = chunk.strip()
        if not chunk:
            continue
        merchant_id, has_name, name = chunk.partition("=")
        merchant_id = merchant_id.strip()
        if not merchant_id:
            continue
        entries.append({"id": merchant_id, "name": name.strip() if has_name else ""})
    return entries


def local_merchant_catalog(settings: Any) -> list[dict[str, str]]:
    """The merchants the local bypass offers, primary first and de-duplicated by id."""
    primary_id = str(getattr(settings, "am_bypass_merchant_id", "") or "").strip()
    catalog: list[dict[str, str]] = []
    seen: set[str] = set()
    if primary_id:
        primary_name = (
            str(getattr(settings, "am_bypass_merchant_name", "") or "").strip()
            or DEFAULT_MERCHANT_NAME
        )
        catalog.append({"id": primary_id, "name": primary_name})
        seen.add(primary_id)
    raw = str(getattr(settings, "am_bypass_merchants", "") or "")
    for entry in parse_merchant_entries(raw):
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])
        catalog.append({"id": entry["id"], "name": entry["name"] or entry["id"]})
    return catalog


def local_merchant_name(settings: Any, merchant_id: str) -> str:
    """Display name for ``merchant_id``; unknown ids keep the configured default."""
    for entry in local_merchant_catalog(settings):
        if entry["id"] == merchant_id:
            return entry["name"]
    return (
        str(getattr(settings, "am_bypass_merchant_name", "") or "").strip()
        or DEFAULT_MERCHANT_NAME
    )
