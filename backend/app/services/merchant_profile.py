from __future__ import annotations

from typing import Any

from app.services.am_client import AMClient, AMClientError


def load_profile(am: AMClient, cookie: str, merchant_id: str) -> dict[str, Any] | None:
    try:
        return am.profile(cookie, merchant_id)
    except AMClientError as exc:
        if exc.status_code == 404:
            return None
        raise


def project_profile(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    if not profile:
        return None
    merchant_id = str(profile.get("merchant_id") or "")
    brand = profile.get("brand") if isinstance(profile.get("brand"), dict) else {}
    sections: list[dict[str, Any]] = []
    canonical_name = str(brand.get("canonical_name") or "")
    identity = {
        key: brand.get(key)
        for key in (
            "legal_name",
            "canonical_name",
            "primary_category",
            "secondary_categories",
            "cuisine",
            "positioning",
            "personality",
            "voice",
        )
    }
    if any(value not in (None, "", [], {}) for value in identity.values()):
        sections.append(_section("品牌信息", identity, "brand"))
    if brand.get("positioning"):
        sections.append(_section("品牌定位", brand.get("positioning"), "brand.positioning"))
    if brand.get("personality"):
        sections.append(_section("品牌性格", brand.get("personality"), "brand.personality"))
    voice = brand.get("voice")
    if isinstance(voice, dict) and voice.get("description"):
        sections.append(_section("品牌语气", voice.get("description"), "brand.voice.description"))

    for index, location in enumerate(profile.get("locations") or []):
        if not isinstance(location, dict):
            continue
        place_id = str(location.get("place_id") or f"location-{index + 1}")
        values = {
            "place_id": place_id,
            "name": location.get("name"),
            "core_highlights": location.get("core_highlights"),
            "dishes": location.get("dishes"),
            "competitive_context": location.get("competitive_context"),
        }
        sections.append(_section(f"门店卖点（{location.get('name') or place_id}）", values, f"locations[{index}]"))

    return {
        "id": merchant_id,
        "name": canonical_name or str(profile.get("merchant_name") or merchant_id),
        "source_label": "AM Console · merchant-profile · ai-marketing restaurant_brand_profiles",
        "source_record_id": merchant_id,
        "sections": sections,
        "raw_brand": _safe_raw(brand),
        "location_place_ids": [
            str(item.get("place_id"))
            for item in profile.get("locations") or []
            if isinstance(item, dict) and item.get("place_id")
        ],
    }


def _section(title: str, value: Any, source_path: str) -> dict[str, Any]:
    return {
        "title": title,
        "text": _display_text(value),
        "source_path": source_path,
        "original": value,
    }


def _display_text(value: Any) -> str:
    if value is None or value == "" or value == [] or value == {}:
        return "资料未提供"
    if isinstance(value, list):
        return "；".join(str(item) for item in value if item not in (None, ""))
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            if item in (None, "", [], {}):
                continue
            if isinstance(item, list):
                parts.append(f"{key}: {'；'.join(str(value) for value in item)}")
            elif isinstance(item, dict):
                parts.append(f"{key}: {item}")
            else:
                parts.append(f"{key}: {item}")
        return "；".join(parts) or "资料未提供"
    return str(value)


def _safe_raw(brand: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "legal_name", "canonical_name", "name_variants", "primary_category",
        "secondary_categories", "cuisine", "positioning", "personality", "voice",
    )
    return {key: brand.get(key) for key in allowed if key in brand}
