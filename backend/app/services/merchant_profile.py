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
        "brand_info": _brand_info(brand, profile.get("locations") or []),
        "location_place_ids": [
            str(item.get("place_id"))
            for item in profile.get("locations") or []
            if isinstance(item, dict) and item.get("place_id")
        ],
    }



def _brand_info(brand: dict[str, Any], locations: list[Any]) -> dict[str, Any]:
    """The facts a reader of the style page needs, kept out of the model background.

    The year is brand-wide; address and phone belong to each store, so they stay per
    location rather than collapsing to whichever store happens to be first.
    """
    stores = []
    for location in locations:
        if not isinstance(location, dict):
            continue
        nap = location.get("nap") if isinstance(location.get("nap"), dict) else {}
        stores.append(
            {
                "place_id": str(location.get("place_id") or ""),
                "name": str(location.get("name") or ""),
                "address": str(nap.get("address") or "").strip(),
                "suite": str(nap.get("suite") or "").strip(),
                "phone": str(nap.get("phone") or "").strip(),
            }
        )
    return {
        "year_established": brand.get("year_established"),
        "stores": stores,
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
        # Brand-level facts the info card prints; not part of the model background.
        "year_established",
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


def stored_profile(db: Any, merchant_id: str | None) -> dict[str, Any] | None:
    """The profile previously imported from AM Console, for local runs without a session.

    The stored record keeps the AM payload under `source`, so the local view is the same
    projection the console would produce instead of a second, drifting shape.
    """
    if not merchant_id:
        return None
    record = db.merchant_profiles.find_one({"_id": merchant_id})
    if record is None:
        return None
    source = record.get("source")
    if not isinstance(source, dict):
        return get_profile(db, merchant_id)
    return project_profile({"merchant_id": str(record.get("_id") or merchant_id), **source})


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


def store_facts(
    profile: dict[str, Any] | None,
    place_id: str,
    *,
    include_address: bool,
    include_phone: bool,
) -> dict[str, str] | None:
    """The selected store facts a poster may print, or None when there is nothing to add.

    Only the requested fields come back, so a merchant that may not show a phone can still put
    its address on the poster.
    """
    if not profile or not place_id:
        return None
    for location in profile.get("locations") or []:
        if not isinstance(location, dict) or str(location.get("place_id") or "") != place_id:
            continue
        nap = location.get("nap") if isinstance(location.get("nap"), dict) else {}
        facts: dict[str, str] = {"name": str(location.get("name") or "").strip()}
        if include_address:
            address = str(nap.get("address") or "").strip()
            suite = str(nap.get("suite") or "").strip()
            facts["address"] = f"{address} {suite}".strip()
        if include_phone:
            facts["phone"] = str(nap.get("phone") or "").strip()
        # A store with neither requested field filled in has nothing to say.
        if not facts.get("address") and not facts.get("phone"):
            return None
        return facts
    return None


def store_context(facts: dict[str, str] | None) -> str:
    """One block the model may print verbatim; it must not invent or reformat these values."""
    if not facts:
        return ""
    lines = ["门店信息（可以放在海报上，必须逐字照抄，不得改写、缩写或编造）："]
    if facts.get("name"):
        lines.append("门店：" + facts["name"])
    if facts.get("address"):
        lines.append("地址：" + facts["address"])
    if facts.get("phone"):
        lines.append("电话：" + facts["phone"])
    return "\n".join(lines)


def prompt_context(
    profile: dict[str, Any] | None,
    store: dict[str, str] | None = None,
) -> str:
    if not profile:
        return store_context(store)
    blocks: list[str] = []
    if profile.get("prompt_sections"):
        blocks.append(
            "\n".join(
                (
                    "商户背景参考：" + str(profile["name"]),
                    *(section["title"] + "：" + section["text"] for section in profile["prompt_sections"]),
                    "以上为用户提供的背景参考，未独立核验，按本次需求选取相关信息，不必全部写入海报。"
                    "本次要求和备注优先，同时遵守平台限制。不要默认添加地址、电话或营业时间。",
                )
            )
        )
    store_block = store_context(store)
    if store_block:
        blocks.append(store_block)
    return "\n\n".join(blocks)
