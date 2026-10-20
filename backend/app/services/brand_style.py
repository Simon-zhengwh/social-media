from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BRAND_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
HEX_COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}

DEFAULT_PALETTE = {
    "primary": "#0D4317",
    "primary_deep": "#08300F",
    "accent": "#B12310",
    "accent_bright": "#D92D20",
    "accent_deep": "#7A160F",
    "cream": "#FFF4DF",
    "highlight": "#FFE36B",
    "ink": "#3A2318",
    "white": "#FFFFFF",
    "cta_text": "#153F2A",
}

DEFAULT_FONTS = {"display": "brush", "body": "sans", "price": "sans"}

FALLBACK_LAYOUT_GUIDE = (
    "目标是高转化的中式餐饮社媒促销海报。真实菜品占据画面主体，"
    "品牌色强烈，超大粗体中文标题，清晰的价格/优惠层级，"
    "信息密度高但不遮挡菜品；Logo 小而精致，透明融入画面，不使用卡片底板。"
)

FALLBACK_BACKGROUND_GUIDE = (
    "商业餐饮摄影底图。真实菜品为主体，保留外观、数量、摆盘、质感和细节；"
    "暖色灯光、层次丰富、有食欲，在画面上部和下部留出干净区域。"
)


class BrandError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class BrandStyle:
    id: str
    name: str
    short_name: str
    summary: str
    version: int
    is_default: bool
    cover_path: Path | None
    logo_keywords: tuple[str, ...]
    palette: dict[str, str] = field(default_factory=dict)
    fonts: dict[str, str] = field(default_factory=dict)
    layout_guide: str = FALLBACK_LAYOUT_GUIDE
    background_guide: str = FALLBACK_BACKGROUND_GUIDE
    reference_paths: tuple[Path, ...] = ()

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "short_name": self.short_name,
            "summary": self.summary,
            "version": self.version,
            "palette": dict(self.palette),
            "fonts": dict(self.fonts),
            "layout_guide": self.layout_guide,
            "background_guide": self.background_guide,
            "logo_keywords": list(self.logo_keywords),
        }

    def public_dict(self, *, cover_url: str | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "short_name": self.short_name,
            "summary": self.summary,
            "version": self.version,
            "is_default": self.is_default,
            "cover_url": cover_url,
            "logo_keywords": list(self.logo_keywords),
            "palette": dict(self.palette),
            "fonts": dict(self.fonts),
        }


def _default_brand() -> BrandStyle:
    return BrandStyle(
        id="seejoy",
        name="SEEJOY 喜聚",
        short_name="喜聚",
        summary="深森林绿、品牌红与奶油白的餐饮促销海报。",
        version=1,
        is_default=True,
        cover_path=None,
        logo_keywords=("seejoy", "喜聚"),
        palette=dict(DEFAULT_PALETTE),
        fonts=dict(DEFAULT_FONTS),
    )


def _safe_path(root: Path, relative: Any, *, label: str, must_exist: bool = True) -> Path:
    if not isinstance(relative, str) or not relative.strip():
        raise BrandError("invalid_brand", f"{label} 路径为空")
    candidate = Path(relative.strip())
    if candidate.is_absolute():
        raise BrandError("invalid_brand", f"{label} 必须使用 skill 内相对路径")
    root_resolved = root.resolve()
    resolved = (root_resolved / candidate).resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise BrandError("invalid_brand", f"{label} 路径越出 skill 目录")
    if must_exist and not resolved.is_file():
        raise BrandError("invalid_brand", f"{label} 文件不存在：{relative}")
    return resolved


def _read_text(path: Path, *, label: str) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise BrandError("invalid_brand", f"{label} 无法读取") from exc
    if not text:
        raise BrandError("invalid_brand", f"{label} 为空")
    return text


def _normalize_palette(raw: Any) -> dict[str, str]:
    palette = dict(DEFAULT_PALETTE)
    if not isinstance(raw, dict):
        return palette
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, str) and HEX_COLOR_PATTERN.fullmatch(value):
            palette[key] = value.upper()
    return palette


def _normalize_fonts(raw: Any) -> dict[str, str]:
    fonts = dict(DEFAULT_FONTS)
    if not isinstance(raw, dict):
        return fonts
    for key in ("display", "body", "price"):
        value = raw.get(key)
        if value in {"sans", "brush"}:
            fonts[key] = value
    return fonts


def load_brand_skill(skill_dir: Path) -> BrandStyle:
    skill_dir = skill_dir.resolve()
    manifest = skill_dir / "brand.json"
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BrandError("invalid_brand", "brand.json 不存在或无法读取") from exc
    except json.JSONDecodeError as exc:
        raise BrandError("invalid_brand", "brand.json 不是合法 JSON") from exc
    if not isinstance(raw, dict):
        raise BrandError("invalid_brand", "brand.json 必须是对象")

    brand_id = str(raw.get("id") or "").strip().lower()
    if not BRAND_ID_PATTERN.fullmatch(brand_id):
        raise BrandError("invalid_brand", "品牌 id 只能包含小写字母、数字和连字符")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise BrandError("invalid_brand", "品牌名称不能为空")

    guides = raw.get("guides")
    if not isinstance(guides, dict):
        raise BrandError("invalid_brand", "缺少 guides 配置")
    layout_path = _safe_path(skill_dir, guides.get("layout"), label="layout 指南")
    background_path = _safe_path(
        skill_dir, guides.get("background"), label="background 指南"
    )
    cover_raw = raw.get("cover")
    cover_path = (
        _safe_path(skill_dir, cover_raw, label="封面图") if cover_raw else None
    )

    assets_dir = (skill_dir / "assets").resolve()
    reference_paths: list[Path] = []
    if assets_dir.is_dir():
        reference_paths = sorted(
            path.resolve()
            for path in assets_dir.iterdir()
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        )
    if cover_path is None and reference_paths:
        cover_path = reference_paths[0]

    keywords_raw = raw.get("logo_keywords")
    keywords = tuple(
        str(item).strip().lower()
        for item in keywords_raw
        if isinstance(item, str) and item.strip()
    ) if isinstance(keywords_raw, list) else ()

    try:
        version = int(raw.get("version") or 1)
    except (TypeError, ValueError):
        version = 1

    return BrandStyle(
        id=brand_id,
        name=name,
        short_name=str(raw.get("short_name") or name).strip(),
        summary=str(raw.get("summary") or "").strip(),
        version=max(1, version),
        is_default=bool(raw.get("is_default")),
        cover_path=cover_path,
        logo_keywords=keywords,
        palette=_normalize_palette(raw.get("palette")),
        fonts=_normalize_fonts(raw.get("fonts")),
        layout_guide=_read_text(layout_path, label="layout 指南"),
        background_guide=_read_text(background_path, label="background 指南"),
        reference_paths=tuple(reference_paths[:3]),
    )


@lru_cache(maxsize=8)
def _discover_cached(skills_dir: str) -> tuple[BrandStyle, ...]:
    root = Path(skills_dir)
    if not root.is_dir():
        logger.warning("brand skills directory is missing: %s", root)
        return ()

    brands: dict[str, BrandStyle] = {}
    for manifest in sorted(root.glob("*/brand.json")):
        try:
            brand = load_brand_skill(manifest.parent)
        except BrandError as exc:
            logger.warning("skip invalid brand skill %s: %s", manifest.parent, exc.message)
            continue
        if brand.id in brands:
            logger.warning(
                "skip duplicate brand id %s at %s", brand.id, manifest.parent
            )
            continue
        brands[brand.id] = brand

    return tuple(
        sorted(
            brands.values(),
            key=lambda item: (not item.is_default, item.name.lower(), item.id),
        )
    )


def list_brands(skills_dir: Path) -> list[BrandStyle]:
    return list(_discover_cached(str(skills_dir)))


def clear_brand_cache() -> None:
    _discover_cached.cache_clear()


def get_brand(skills_dir: Path, brand_id: str | None = None) -> BrandStyle:
    brands = list_brands(skills_dir)
    if brand_id:
        for brand in brands:
            if brand.id == brand_id:
                return brand
        raise BrandError("brand_not_found", "品牌风格不存在")
    for brand in brands:
        if brand.is_default:
            return brand
    if brands:
        return brands[0]
    logger.warning("no brand skills found; using built-in fallback brand")
    return _default_brand()


def brand_from_snapshot(snapshot: Any) -> BrandStyle | None:
    if not isinstance(snapshot, dict):
        return None
    brand_id = str(snapshot.get("id") or "").strip()
    if not brand_id:
        return None
    palette = _normalize_palette(snapshot.get("palette"))
    fonts = _normalize_fonts(snapshot.get("fonts"))
    layout_guide = str(snapshot.get("layout_guide") or "").strip()
    background_guide = str(snapshot.get("background_guide") or "").strip()
    if not layout_guide or not background_guide:
        return None
    try:
        version = int(snapshot.get("version") or 1)
    except (TypeError, ValueError):
        version = 1
    keywords_raw = snapshot.get("logo_keywords")
    keywords = tuple(
        str(item).strip().lower()
        for item in keywords_raw
        if isinstance(item, str) and item.strip()
    ) if isinstance(keywords_raw, list) else ()
    return BrandStyle(
        id=brand_id,
        name=str(snapshot.get("name") or brand_id),
        short_name=str(snapshot.get("short_name") or snapshot.get("name") or brand_id),
        summary=str(snapshot.get("summary") or ""),
        version=max(1, version),
        is_default=False,
        cover_path=None,
        logo_keywords=keywords,
        palette=palette,
        fonts=fonts,
        layout_guide=layout_guide,
        background_guide=background_guide,
    )


def brand_for_task(task: dict[str, Any], skills_dir: Path) -> BrandStyle:
    snapshot = brand_from_snapshot(task.get("brand"))
    if snapshot is not None:
        return snapshot
    input_data = task.get("input") or {}
    brand_id = input_data.get("brand_id")
    try:
        return get_brand(skills_dir, str(brand_id) if brand_id else None)
    except BrandError:
        logger.warning("task brand %s is unavailable; using fallback", brand_id)
        return _default_brand()
