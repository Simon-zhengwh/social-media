from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services import storage
from app.services.image_ref import ImageRef, image_ref_from_asset

logger = logging.getLogger(__name__)

BRAND_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
MAX_BRAND_REFERENCES = 5
IMAGE_SUFFIXES = storage.SUPPORTED_IMAGE_SUFFIXES

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
    is_default: bool
    cover_path: Path | None
    logo_keywords: tuple[str, ...]
    platform: str = ""
    note: str = ""
    layout_guide: str = FALLBACK_LAYOUT_GUIDE
    background_guide: str = FALLBACK_BACKGROUND_GUIDE
    reference_paths: tuple[Path, ...] = ()
    cover_asset_id: str | None = None
    reference_asset_ids: tuple[str, ...] = ()
    reference_inputs: tuple[ImageRef, ...] = ()

    def snapshot(self, references: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "short_name": self.short_name,
            "summary": self.summary,
            "layout_guide": self.layout_guide,
            "background_guide": self.background_guide,
            "logo_keywords": list(self.logo_keywords),
            "cover_asset_id": self.cover_asset_id,
            "reference_asset_ids": list(self.reference_asset_ids),
            "references": list(references or []),
        }

    def public_dict(self, *, cover_url: str | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "short_name": self.short_name,
            "summary": self.summary,
            "platform": self.platform,
            "note": self.note,
            "is_default": self.is_default,
            "cover_url": cover_url,
            "logo_keywords": list(self.logo_keywords),
        }


def _default_brand() -> BrandStyle:
    return BrandStyle(
        id="seejoy",
        name="SEEJOY 喜聚",
        short_name="喜聚",
        summary="深森林绿、品牌红与奶油白的餐饮促销海报。",
        is_default=True,
        cover_path=None,
        logo_keywords=("seejoy", "喜聚"),
    )


def default_brand() -> BrandStyle:
    return _default_brand()


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
    except (OSError, UnicodeError) as exc:
        raise BrandError("invalid_brand", f"{label} 无法读取") from exc
    if not text:
        raise BrandError("invalid_brand", f"{label} 为空")
    return text


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _normalize_keywords(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(
        dict.fromkeys(
            str(item).strip().lower()
            for item in raw
            if isinstance(item, str) and item.strip()
        )
    )


def _normalize_asset_ids(raw: Any, *, limit: int = MAX_BRAND_REFERENCES) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    ids: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        ids.append(value)
        if len(ids) >= limit:
            break
    return tuple(ids)


def load_brand_skill(skill_dir: Path) -> BrandStyle:
    """Load a brand skill from disk. Runtime code does not call this; import uses it."""
    skill_dir = skill_dir.resolve()
    manifest = skill_dir / "brand.json"
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
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

    return BrandStyle(
        id=brand_id,
        name=name,
        short_name=str(raw.get("short_name") or name).strip(),
        summary=str(raw.get("summary") or "").strip(),
        platform=str(raw.get("platform") or "").strip(),
        note=str(raw.get("note") or "").strip(),
        is_default=_as_bool(raw.get("is_default")),
        cover_path=cover_path,
        logo_keywords=_normalize_keywords(raw.get("logo_keywords")),
        layout_guide=_read_text(layout_path, label="layout 指南"),
        background_guide=_read_text(background_path, label="background 指南"),
        reference_paths=tuple(reference_paths[:MAX_BRAND_REFERENCES]),
    )


def brand_from_document(document: Any) -> BrandStyle:
    if not isinstance(document, dict):
        raise BrandError("invalid_brand", "品牌配置必须是对象")
    brand_id = str(document.get("_id") or document.get("id") or "").strip().lower()
    if not BRAND_ID_PATTERN.fullmatch(brand_id):
        raise BrandError("invalid_brand", "品牌 id 只能包含小写字母、数字和连字符")
    name = str(document.get("name") or "").strip()
    if not name:
        raise BrandError("invalid_brand", "品牌名称不能为空")
    layout_guide = str(document.get("layout_guide") or "").strip()
    background_guide = str(document.get("background_guide") or "").strip()
    if not layout_guide or not background_guide:
        raise BrandError("invalid_brand", "品牌排版指南和背景指南不能为空")
    return BrandStyle(
        id=brand_id,
        name=name,
        short_name=str(document.get("short_name") or name).strip(),
        summary=str(document.get("summary") or "").strip(),
        platform=str(document.get("platform") or "").strip(),
        note=str(document.get("note") or "").strip(),
        is_default=_as_bool(document.get("is_default")),
        cover_path=None,
        logo_keywords=_normalize_keywords(document.get("logo_keywords")),
        layout_guide=layout_guide,
        background_guide=background_guide,
        cover_asset_id=str(document.get("cover_asset_id") or "").strip() or None,
        reference_asset_ids=_normalize_asset_ids(document.get("reference_asset_ids")),
    )


def list_brands(db: Any) -> list[BrandStyle]:
    if db is None:
        return []
    brands: list[BrandStyle] = []
    for document in db.brands.find({}):
        try:
            brands.append(brand_from_document(document))
        except Exception as exc:  # noqa: BLE001 - one bad brand must not hide the rest
            message = exc.message if isinstance(exc, BrandError) else str(exc)
            logger.warning("skip invalid brand document %s: %s", document.get("_id"), message)
    return sorted(
        brands,
        key=lambda item: (not item.is_default, item.name.lower(), item.id),
    )


def get_brand(db: Any, brand_id: str | None = None) -> BrandStyle:
    if db is None:
        raise BrandError("brand_not_found", "品牌数据库不可用")
    if brand_id:
        document = db.brands.find_one({"_id": brand_id})
        if document is None:
            raise BrandError("brand_not_found", "品牌风格不存在")
        return brand_from_document(document)
    brands = list_brands(db)
    if not brands:
        raise BrandError("brand_not_found", "没有可用的品牌配置")
    for brand in brands:
        if brand.is_default:
            return brand
    return brands[0]


def brand_from_snapshot(snapshot: Any) -> BrandStyle | None:
    if not isinstance(snapshot, dict):
        return None
    brand_id = str(snapshot.get("id") or "").strip()
    if not brand_id:
        return None
    layout_guide = str(snapshot.get("layout_guide") or "").strip()
    background_guide = str(snapshot.get("background_guide") or "").strip()
    if not layout_guide or not background_guide:
        return None
    reference_asset_ids = _normalize_asset_ids(snapshot.get("reference_asset_ids"))
    if not reference_asset_ids:
        reference_asset_ids = _normalize_asset_ids(
            [
                item.get("asset_id")
                for item in snapshot.get("references") or []
                if isinstance(item, dict)
            ]
        )
    cover_asset_id = str(snapshot.get("cover_asset_id") or "").strip() or None
    if cover_asset_id is None and reference_asset_ids:
        cover_asset_id = reference_asset_ids[0]
    return BrandStyle(
        id=brand_id,
        name=str(snapshot.get("name") or brand_id),
        short_name=str(snapshot.get("short_name") or snapshot.get("name") or brand_id),
        summary=str(snapshot.get("summary") or ""),
        is_default=False,
        cover_path=None,
        logo_keywords=_normalize_keywords(snapshot.get("logo_keywords")),
        layout_guide=layout_guide,
        background_guide=background_guide,
        cover_asset_id=cover_asset_id,
        reference_asset_ids=reference_asset_ids,
    )


def _reference_entry(asset: dict[str, Any]) -> dict[str, Any]:
    return {
        "asset_id": asset["_id"],
        "sha256": asset.get("sha256") or "",
        "original_name": asset.get("original_name") or "",
        "width": int(asset.get("width") or 0),
        "height": int(asset.get("height") or 0),
    }


def _asset_for_path(
    db: Any,
    store: Any,
    settings: Any,
    path: Path,
    *,
    kind: str = "brand_reference",
) -> dict[str, Any]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BrandError("brand_reference_missing", f"品牌参考图无法读取：{path.name}") from exc
    digest = hashlib.sha256(data).hexdigest()
    asset = db.assets.find_one({"kind": kind, "sha256": digest})
    if asset is not None:
        return asset
    prepared = storage.prepare_upload(settings, store, data, path.name, kind)
    document = {
        "_id": prepared.id,
        "kind": prepared.kind,
        "original_name": prepared.original_name,
        "path": prepared.path,
        "prepared_path": prepared.prepared_path,
        "mime_type": prepared.mime_type,
        "width": prepared.width,
        "height": prepared.height,
        "bytes": prepared.bytes,
        "sha256": prepared.sha256,
        "created_at": datetime.now(timezone.utc),
    }
    db.assets.insert_one(document)
    return document


def snapshot_brand(
    brand: BrandStyle,
    db: Any,
    store: Any,
    settings: Any,
) -> dict[str, Any]:
    """Create an immutable task snapshot from Mongo brand assets."""
    references: list[dict[str, Any]] = []
    if brand.reference_asset_ids:
        for asset_id in brand.reference_asset_ids[:MAX_BRAND_REFERENCES]:
            asset = db.assets.find_one({"_id": asset_id})
            if asset is None:
                raise BrandError(
                    "brand_reference_missing",
                    f"品牌参考图不存在：{asset_id}",
                )
            references.append(_reference_entry(asset))
    elif brand.reference_paths:
        # Compatibility path for old code/tests that construct BrandStyle from files.
        for path in brand.reference_paths[:MAX_BRAND_REFERENCES]:
            references.append(
                _reference_entry(_asset_for_path(db, store, settings, path))
            )

    cover_asset_id = brand.cover_asset_id
    if cover_asset_id and db.assets.find_one({"_id": cover_asset_id}) is None:
        logger.warning("brand cover asset missing: %s", cover_asset_id)
        cover_asset_id = None
    if cover_asset_id is None and brand.cover_path is not None:
        cover_asset_id = _asset_for_path(db, store, settings, brand.cover_path)["_id"]
    if cover_asset_id is None and references:
        cover_asset_id = references[0]["asset_id"]

    snapshot = brand.snapshot(references)
    snapshot["cover_asset_id"] = cover_asset_id
    snapshot["reference_asset_ids"] = [item["asset_id"] for item in references]
    return snapshot


def _reference_inputs_from_asset_ids(
    asset_ids: tuple[str, ...],
    db: Any,
    store: Any,
) -> tuple[ImageRef, ...]:
    if db is None or store is None:
        return ()
    references: list[ImageRef] = []
    for asset_id in asset_ids[:MAX_BRAND_REFERENCES]:
        try:
            references.append(image_ref_from_asset(db, store, asset_id))
        except Exception as exc:  # noqa: BLE001 - legacy tasks tolerate missing assets
            logger.warning("brand reference asset unavailable %s: %s", asset_id, exc)
    return tuple(references)


def brand_for_task(
    task: dict[str, Any],
    db: Any | None = None,
    store: Any | None = None,
) -> BrandStyle | None:
    snapshot = brand_from_snapshot(task.get("brand"))
    if snapshot is not None:
        references = _reference_inputs_from_asset_ids(snapshot.reference_asset_ids, db, store)
        return replace(
            snapshot,
            reference_paths=tuple(ref.path for ref in references if ref.path is not None),
            reference_inputs=references,
        )
    raw_brand_id = (task.get("input") or {}).get("brand_id")
    if not raw_brand_id:
        return None
    brand_id = str(raw_brand_id)
    try:
        brand = get_brand(db, brand_id)
    except BrandError:
        return None
    references = _reference_inputs_from_asset_ids(brand.reference_asset_ids, db, store)
    return replace(
        brand,
        reference_paths=tuple(ref.path for ref in references if ref.path is not None),
        reference_inputs=references,
    )


def _import_loaded_brand(
    db: Any,
    store: Any,
    settings: Any,
    brand: BrandStyle,
    *,
    force: bool,
) -> dict[str, Any]:
    existing = db.brands.find_one({"_id": brand.id})
    if existing is not None and not force:
        return {
            "brand_id": brand.id,
            "status": "skipped",
            "reference_count": len(existing.get("reference_asset_ids") or []),
        }

    reference_docs = [
        _asset_for_path(db, store, settings, path)
        for path in brand.reference_paths[:MAX_BRAND_REFERENCES]
    ]
    reference_asset_ids = [document["_id"] for document in reference_docs]
    cover_asset_id: str | None = None
    if brand.cover_path is not None:
        cover_asset_id = _asset_for_path(db, store, settings, brand.cover_path)["_id"]
    if cover_asset_id is None and reference_asset_ids:
        cover_asset_id = reference_asset_ids[0]

    now = datetime.now(timezone.utc)
    document = {
        "_id": brand.id,
        "name": brand.name,
        "short_name": brand.short_name,
        "summary": brand.summary,
        "platform": brand.platform,
        "note": brand.note,
        "is_default": brand.is_default,
        "layout_guide": brand.layout_guide,
        "background_guide": brand.background_guide,
        "logo_keywords": list(brand.logo_keywords),
        "cover_asset_id": cover_asset_id,
        "reference_asset_ids": reference_asset_ids,
        "created_at": (existing or {}).get("created_at") or now,
        "updated_at": now,
    }
    db.brands.replace_one({"_id": brand.id}, document, upsert=True)
    return {
        "brand_id": brand.id,
        "status": "imported",
        "reference_count": len(reference_asset_ids),
        "cover_asset_id": cover_asset_id,
    }


def import_brand_skill(
    db: Any,
    store: Any,
    settings: Any,
    skill_dir: Path,
    *,
    force: bool = False,
) -> dict[str, Any]:
    return _import_loaded_brand(
        db,
        store,
        settings,
        load_brand_skill(skill_dir),
        force=force,
    )


def import_brand_skills(
    db: Any,
    store: Any,
    settings: Any,
    skills_dir: Path,
    *,
    force: bool = False,
    only: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    root = Path(skills_dir)
    result: dict[str, list[dict[str, Any]]] = {
        "imported": [],
        "skipped": [],
        "errors": [],
    }
    if not root.is_dir():
        result["errors"].append(
            {"skill": str(root), "error": "品牌 skill 目录不存在"}
        )
        return result
    for manifest in sorted(root.glob("*/brand.json")):
        try:
            brand = load_brand_skill(manifest.parent)
            if only and brand.id != only:
                continue
            item = _import_loaded_brand(
                db,
                store,
                settings,
                brand,
                force=force,
            )
            result[item["status"]].append(item)
        except Exception as exc:  # noqa: BLE001 - one bad skill must not hide the rest
            message = exc.message if isinstance(exc, BrandError) else str(exc)
            result["errors"].append(
                {"skill": manifest.parent.name, "error": message}
            )
    return result
