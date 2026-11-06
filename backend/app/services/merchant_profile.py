from __future__ import annotations

from typing import Any


PROMPT_SECTIONS = {
    "brand.positioning": "商户定位",
    "locations.0.core_highlights": "核心卖点",
    "locations.0.competitive_context.target_customers": "目标客群",
    "brand.voice.description": "表达语气",
}


def get_profile(db: Any, merchant_id: str | None) -> dict[str, Any] | None:
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
    if not profile or not profile["prompt_sections"]:
        return ""
    return "\n".join((
        f"商户背景参考：{profile['name']}",
        *(f"{section['title']}：{section['text']}" for section in profile["prompt_sections"]),
        "以上为用户提供的背景参考，未独立核验，按本次需求选取相关信息，不必全部写入海报。"
        "本次要求和备注优先，同时遵守平台限制。不要默认添加地址、电话或营业时间。",
    ))
