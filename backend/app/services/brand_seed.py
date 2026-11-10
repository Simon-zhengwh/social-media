"""Seed one brand style per merchant from the AM restaurant brand profile.

The merchant already has a profile in AM (that is why it is selectable in the console), but the
social database starts empty, so every merchant enters the workspace with no platform style. This
module derives a first style from fields AM actually stores, so the operator edits a draft instead
of filling a blank form. Only the name, summary, keywords and logo colours come from the profile;
the visual style fields are neutral defaults the operator is expected to replace.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable

from PIL import Image, ImageOps

from app.services import asset_records, brand_style, merchant_logos, storage

logger = logging.getLogger(__name__)

SEED_SOURCE = "am-merchant-profile"
HEX_PATTERN = re.compile(r"^#[0-9A-F]{6}$")
MAX_COLORS = 6
PALETTE_SAMPLE = 96
NEAR_WHITE_THRESHOLD = 244
PALETTE_DISTANCE = 42

# Neutral starting points. They say "not yet decided", not "this is the brand" — the operator
# replaces them against the reference images they upload.
DEFAULT_STYLE_FIELDS = {
    "typography": "清晰易读的粗体标题字，正文从简；重点信息（菜品、优惠、日期）层级分明。",
    "subject_style": "真实餐饮摄影或真实菜品素材，保留食物原本的形态、颜色、纹理与摆盘；不重绘、不编造。",
    "background": "干净的单色或低饱和背景，为标题和菜品留出明确空间，不抢主体。",
    "layout": "主标题 →主体菜品 →次要信息（时间、条件）。一个视觉重点，文字不遮挡主体。",
    "decoration": "",
    "identity": "",
    "restrictions": "",
}


class SeedError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _text(value: Any) -> str:
    return str(value or "").strip()


def _first_sentence(value: Any, limit: int = 120) -> str:
    text = _text(value)
    if not text:
        return ""
    head = re.split(r"(?<=[.!?。！？])\s+", text, maxsplit=1)[0].strip()
    return head[:limit]


def _summary(brand: dict[str, Any]) -> str:
    parts: list[str] = []
    category = _text(brand.get("primary_category"))
    cuisine = [_text(item) for item in brand.get("cuisine") or [] if _text(item)]
    if category:
        parts.append(category)
    if cuisine:
        parts.append(" / ".join(cuisine[:3]))
    headline = _first_sentence(brand.get("positioning"))
    if headline:
        parts.append(headline)
    return " · ".join(parts)[:500]


def _keywords(profile: dict[str, Any]) -> list[str]:
    brand = profile.get("brand") if isinstance(profile.get("brand"), dict) else {}
    candidates = [_text(brand.get("canonical_name")), _text(brand.get("legal_name"))]
    candidates.extend(_text(item) for item in brand.get("name_variants") or [])
    seen: set[str] = set()
    keywords: list[str] = []
    for item in candidates:
        lowered = item.lower()
        if not lowered or lowered in seen:
            continue
        seen.add(lowered)
        keywords.append(lowered)
        if len(keywords) >= 20:
            break
    return keywords


def _style_fields(brand: dict[str, Any]) -> dict[str, str]:
    fields = dict(DEFAULT_STYLE_FIELDS)
    cuisine = [_text(item) for item in brand.get("cuisine") or [] if _text(item)]
    if cuisine:
        fields["subject_style"] = (
            f"真实{' / '.join(cuisine[:3])}菜品素材，保留食物原本的形态、数量、颜色、纹理与摆盘；"
            "不重绘、不编造原图中不存在的细节。"
        )
    personality = _first_sentence(brand.get("personality"), limit=80)
    if personality:
        fields["identity"] = f"品牌性格参考：{personality}"
    voice = brand.get("voice")
    if isinstance(voice, dict):
        tone = _first_sentence(voice.get("description"), limit=80)
        if tone:
            fields["restrictions"] = f"表达语气：{tone}；保持与品牌语气一致，不自行夸大宣传。"
    return fields


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _is_near_white(rgb: tuple[int, int, int]) -> bool:
    return min(rgb) >= NEAR_WHITE_THRESHOLD


def _distance(left: tuple[int, int, int], right: tuple[int, int, int]) -> float:
    return sum((a - b) **2 for a, b in zip(left, right)) **0.5


def extract_palette(data: bytes) -> list[dict[str, str]]:
    """Pick up to MAX_COLORS dominant colours from an image, skipping its near-white backdrop."""
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
    except Exception: # noqa: BLE001 - an unreadable logo just means no colours
        return []
    image.thumbnail((PALETTE_SAMPLE, PALETTE_SAMPLE), Image.Resampling.LANCZOS)
    counts = image.getcolors(maxcolors=PALETTE_SAMPLE * PALETTE_SAMPLE) or []
    ranked = sorted(counts, key=lambda item: item[0], reverse=True)
    colors: list[tuple[int, int, int]] = []
    for _, rgb in ranked:
        if _is_near_white(rgb):
            continue
        if any(_distance(rgb, chosen) < PALETTE_DISTANCE for chosen in colors):
            continue
        colors.append(rgb)
        if len(colors) >= MAX_COLORS:
            break
    return [{"hex": _rgb_to_hex(rgb)} for rgb in colors]


def _logo_asset(
    db: Any,
    settings: Any,
    store: Any,
    profile: dict[str, Any],
    fetch_image: Callable[[str], bytes] | None,
) -> tuple[list[str], list[dict[str, str]]]:
    brand = profile.get("brand") if isinstance(profile.get("brand"), dict) else {}
    url = _text(brand.get("logo_url"))
    if not url.startswith(("http://", "https://")) or fetch_image is None:
        return [], []
    try:
        data = fetch_image(url)
    except Exception as exc: # noqa: BLE001 - the style is still useful without its logo
        logger.warning("brand seed could not fetch logo %s: %s", url, exc)
        return [], []
    if not data:
        return [], []
    name = url.rsplit("/", 1)[-1].split("?", 1)[0] or "logo.png"
    try:
        asset, _ = asset_records.persist_upload(db, settings, store, data, name)
    except Exception as exc: # noqa: BLE001 - reject a bad logo, keep the style
        logger.warning("brand seed could not store logo %s: %s", url, exc)
        return [], []
    return [str(asset["_id"])], extract_palette(data)


def build_document(
    merchant_id: str,
    profile: dict[str, Any],
    *,
    logo_asset_ids: list[str] | None = None,
    colors: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Derive the brand document. Pure: callers pass in whatever logo/colours they resolved."""
    brand = profile.get("brand") if isinstance(profile.get("brand"), dict) else {}
    name = _text(brand.get("canonical_name"))
    if not name:
        raise SeedError("merchant_profile_incomplete", "该商户的 AM 品牌资料缺少品牌名称，无法生成风格")
    platform_id, placement_id, purpose = brand_style.platform_placement("xiaohongshu", "note")
    now = datetime.now(timezone.utc)
    return {
        "_id": merchant_id,
        "merchant_id": merchant_id,
        "name": name[:80],
        "short_name": name[:40],
        "summary": _summary(brand),
        "schema_version": 2,
        "style_fields": _style_fields(brand),
        "platform": brand_style.platform_name(platform_id),
        "platform_id": platform_id,
        "placement_id": placement_id,
        "purpose": purpose,
        "note": "",
        "colors": list(colors or []),
        "logo_asset_ids": list(logo_asset_ids or []),
        "logo_keywords": _keywords(profile),
        "is_default": True,
        "cover_asset_id": None,
        "reference_asset_ids": [],
        "seeded_from": SEED_SOURCE,
        "created_at": now,
        "updated_at": now,
    }


def initialize(
    db: Any,
    profile: dict[str, Any] | None,
    *,
    settings: Any,
    store: Any,
    merchant_id: str | None = None,
    fetch_image: Callable[[str], bytes] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create the merchant's first style, or return the existing one. Returns (document, created)."""
    if not profile:
        raise SeedError("merchant_profile_missing", "该商户还没有 AM 品牌资料，无法生成风格")
    profile_merchant = _text(profile.get("merchant_id"))
    if merchant_id and profile_merchant and profile_merchant != merchant_id:
        raise SeedError("merchant_profile_mismatch", "AM 品牌资料与当前商户不匹配")
    target = merchant_id or profile_merchant
    if not target:
        raise SeedError("merchant_profile_incomplete", "无法确定商户 ID")

    existing = db.brands.find_one({"_id": target})
    if existing is not None:
        return existing, False

    logo_asset_ids, colors = _logo_asset(db, settings, store, profile, fetch_image)
    document = build_document(target, profile, logo_asset_ids=logo_asset_ids, colors=colors)
    try:
        db.brands.insert_one(document)
    except Exception:
        # A concurrent request won the race: the merchant still ends up with exactly one style.
        existing = db.brands.find_one({"_id": target})
        if existing is not None:
            return existing, False
        raise
    # The seeded logo is a merchant-level asset; the style keeps its legacy copy untouched.
    merchant_logos.seed_if_empty(db, logo_asset_ids)
    return document, True
