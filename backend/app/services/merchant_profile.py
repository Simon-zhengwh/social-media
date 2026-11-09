from __future__ import annotations

from typing import Any

from app.services.am_client import AMClient, AMClientError
from app.services.scoped_db import ScopedDatabase


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
        "prompt_sections": _am_prompt_sections(profile),
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
        # Included so a first style can adopt the merchant logo.
        "logo_url",
    )
    return {key: brand.get(key) for key in allowed if key in brand}


def _am_prompt_sections(profile: dict[str, Any]) -> list[dict[str, str]]:
    sections = []
    for path, title in PROMPT_SECTIONS.items():
        value: Any = profile
        for part in path.split("."):
            if isinstance(value, dict):
                value = value.get(part)
            elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
                value = value[int(part)]
            else:
                value = None
                break
        if value in (None, "", [], {}):
            continue
        text = _display_text(value).strip()
        if text:
            sections.append({"title": title, "text": text})
    return sections

PROMPT_SECTIONS = {
    "brand.positioning": "商户定位",
    "locations.0.core_highlights": "核心卖点",
    "locations.0.competitive_context.target_customers": "目标客群",
    "brand.voice.description": "表达语气",
}


def get_profile(db: Any, merchant_id: str | None) -> dict[str, Any] | None:
    # UAT profiles come only from the authenticated AM request, never local seeds.
    if isinstance(db, ScopedDatabase):
        return None
    profile = db.merchant_profiles.find_one({"_id": merchant_id}) if merchant_id else None
    if profile is None:
        return None
    texts = {section.get("source_path"): str(section.get("text") or "").strip()
             for section in profile.get("sections") or []}
    sections = [{"title": title, "text": texts[path]}
                for path, title in PROMPT_SECTIONS.items() if texts.get(path)]
    # Keep the existing detail API fields; display and model input use prompt_sections.
    return {
        "id": str(profile["_id"]), "name": profile.get("name", ""),
        "source_label": profile.get("source_label", ""),
        "source_record_id": profile.get("source_record_id", ""),
        "sections": profile.get("sections", []), "prompt_sections": sections,
    }


def prompt_context(profile: dict[str, Any] | None) -> str:
    if not profile or not profile.get("prompt_sections"):
        return ""
    return "\n".join((
        f"商户背景参考：{profile['name']}",
        *(f"{section['title']}：{section['text']}" for section in profile["prompt_sections"]),
        "以上为用户提供的背景参考，未独立核验，按本次需求选取相关信息，不必全部写入海报。"
        "本次要求和备注优先，同时遵守平台限制。不要默认添加地址、电话或营业时间。",
    ))
