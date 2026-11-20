"""Poster-series orchestration.

A series round keeps every page as an ordinary ``whole_tasks`` document so the existing
generation, version, edit, original-replacement and finalization machinery keeps working.
This module owns only the series layer: workspace mode locking, the run/group/page tree,
the deterministic page planner, per-page rendering and the group downloads.

It is opt-in through ``POSTER_SERIES_ENABLED``. With the flag off no series document is
written and the single-poster code paths behave exactly as before.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
from copy import deepcopy
import logging
import math
import re
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
from threading import Lock
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageDraw, ImageOps
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.config import Settings
from app.schemas import PosterSeriesRunCreate, WholeImageTaskCreate, gateway_model_name
from app.services import brand_rules, platform_presets, poster_costs, storage
from app.services.canvas import gateway_image_size
from app.services.image_ref import ImageRef, cleanup_image_refs, image_ref_from_asset
from app.services.whole_image import (
    StudioError,
    _append_message,
    _commit_version,
    _delete_asset,
    _error_text,
    _save_output_asset,
    _version_document,
    _version_outputs,
    utcnow,
)

logger = logging.getLogger(__name__)

SERIES_MODES = {"single", "series"}
PAGE_ROLES = {"cover", "detail"}
_PLAN_LOCKS = tuple(Lock() for _ in range(32))
_ROUND_WORKERS = 8

# A Logo file is transparent artwork, so a light mark over a light poster looks missing even
# though its pixels were composited. These values drive an alpha-weighted legibility
# heuristic that keeps the official file and its box untouched and only adds a neutral
# plate behind poor-contrast marks. _LOGO_CONTRAST_TARGET is a visual threshold, not a
# conformance claim.
_LOGO_CONTRAST_TARGET = 3.0
_LOGO_INK_ALPHA = 96  # 0-255 alpha at or above which a pixel counts as visible ink
_PLATE_DARK = (26, 26, 28)
_PLATE_LIGHT = (250, 250, 247)
_PLATE_OPACITIES = (0.82, 0.92, 1.0)

_COVER_SYSTEM_PROMPT = (
    "你正在生成一组海报设计系列图中的封面。封面确定本组的主题、标题和主视觉，"
    "可以只使用一张主视觉，未指定的设计细节自主决定。仅把用户明确指定的上图文案逐字呈现，"
    "设计指令不要印在图上。只输出最终成图。"
)
_DETAIL_SYSTEM_PROMPT = (
    "你正在生成一组海报设计系列图中的介绍页。延续本组封面的配色、字形风格和取景方式，"
    "但本页要介绍自己的内容，不要重复封面文案。仅把用户明确指定的上图文案逐字呈现，"
    "设计指令不要印在图上。只输出最终成图。"
)
_FOOD_RULE = brand_rules.ASSET_PRESERVATION_RULE

_CN_DIGITS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


class SeriesError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def enabled(settings: Settings | Any) -> bool:
    return bool(getattr(settings, "poster_series_enabled", False))


def require_enabled(settings: Settings | Any) -> None:
    if not enabled(settings):
        raise SeriesError("series_disabled", "系列图功能未启用", 404)


def _merchant_id(db: Any) -> str:
    return str(getattr(db, "merchant_id", "") or "")


MIN_SERIES_PAGES = 2  # a cover plus at least one interior page


def _bounded(value: int, settings: Settings | Any) -> int:
    """Reject out-of-range page counts instead of silently changing them.

    The requirement is the user's text, so a count above the configured maximum has to
    reach the user as an error: quietly rendering fewer pages than asked for looks like
    a successful run with missing pages.
    """
    maximum = int(getattr(settings, "poster_series_max_pages", 12) or 12)
    count = int(value)
    if count < MIN_SERIES_PAGES or count > maximum:
        raise SeriesError(
            "pages_per_group_limit",
            f"每组页数需在 {MIN_SERIES_PAGES}–{maximum} 页之间，当前需求解析为 {count} 页",
            422,
        )
    return count


def _number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    if value in _CN_DIGITS:
        return _CN_DIGITS[value]
    if value.startswith("十") and len(value) == 2 and value[1] in _CN_DIGITS:
        return 10 + _CN_DIGITS[value[1]]
    if len(value) == 2 and value[0] in _CN_DIGITS and value[1] == "十":
        return _CN_DIGITS[value[0]] * 10
    if len(value) == 3 and value[0] in _CN_DIGITS and value[1] == "十" and value[2] in _CN_DIGITS:
        return _CN_DIGITS[value[0]] * 10 + _CN_DIGITS[value[2]]
    return None


_NUMBER = r"([0-9]+|[一二两三四五六七八九十]{1,3})"


def resolve_page_count(
    prompt: str,
    explicit: int | None = None,
    settings: Settings | Any = None,
) -> int:
    """Resolve pages per group from the user's requirement.

    ``做 4 张`` -> 4 pages, ``做 2 张内页`` -> 3 pages (cover + 2 interiors).
    A number that describes content (``介绍 5 种甜品``) is never treated as a page count.
    The default is two pages: one cover and one interior.
    """
    default = int(getattr(settings, "poster_series_default_pages", 2) or 2) if settings else 2
    bound = (lambda count: _bounded(count, settings)) if settings else (lambda count: max(2, count))
    if explicit is not None:
        return bound(int(explicit))
    text = str(prompt or "")
    # Interior-only counts first: they exclude the cover.
    interior = re.search(_NUMBER + r"\s*(?:张|页)\s*内页", text)
    if interior:
        count = _number(interior.group(1))
        if count:
            return bound(count + 1)
    total = re.search(
        r"(?:每组|一组|做|要|共|一共|生成|出)\s*" + _NUMBER + r"\s*(?:张|页|p)",
        text,
        flags=re.IGNORECASE,
    )
    if total:
        count = _number(total.group(1))
        if count:
            return bound(count)
    return bound(default)


_POSITION_WORDS = (
    ("左上", "top-left"), ("左下", "bottom-left"),
    ("右上", "top-right"), ("右下", "bottom-right"),
    ("居中", "center"), ("顶部", "top"), ("底部", "bottom"),
    ("左侧", "left"), ("右侧", "right"),
)

_LOGO_WORDS = re.compile(r"logo|标识|标志|商标", re.IGNORECASE)
_LOGO_CLAUSE_SPLIT = re.compile(r"[，,。;；\n]+")
_SMALL_SIZE = re.compile(r"小(?:一点|一些|号)?|mini|small", re.IGNORECASE)
_LARGE_SIZE = re.compile(r"大(?:一点|一些|号)?|large|big", re.IGNORECASE)


def _logo_clauses(prompt: str) -> list[str]:
    """Return only the clauses that actually talk about the Logo.

    Scanning the whole prompt treats phrases like "做小红书图文，右上角是标题" or "大标题" as
    Logo placement/size overrides. Only a clause that names the Logo may do that.
    """
    return [
        clause.strip() for clause in _LOGO_CLAUSE_SPLIT.split(str(prompt or ""))
        if clause.strip() and _LOGO_WORDS.search(clause)
    ]


def _place_logo_box(
    canvas: dict[str, Any], width: int, height: int, position: str, margin: int
) -> tuple[int, int]:
    canvas_width, canvas_height = int(canvas["width"]), int(canvas["height"])
    if "left" in position:
        x = margin
    elif "right" in position:
        x = canvas_width - margin - width
    else:
        x = (canvas_width - width) // 2
    if "top" in position:
        y = margin
    elif "bottom" in position:
        y = canvas_height - margin - height
    else:
        y = (canvas_height - height) // 2
    return max(0, min(canvas_width - width, x)), max(0, min(canvas_height - height, y))


def _logo_box_size(canvas: dict[str, Any], size: str) -> tuple[int, int]:
    width = int(canvas.get("width") or 0)
    ratio = {"small": 0.10, "medium": 0.14, "large": 0.20}[size]
    box_width = max(24, int(round(width * ratio)))
    # Official logos are usually wider than tall; keep a usable slot and preserve ratio later.
    box_height = max(16, int(round(box_width / 3)))
    return box_width, box_height


def resolve_logo_box(canvas: dict[str, Any], prompt: str) -> dict[str, Any]:
    """Deterministic logo box shared by every page of a group.

    The model never draws the official logo; the backend composites the original file
    into this box. Position and size hints are honoured only when they appear in a Logo
    clause, and each property is reported separately so an unspecified one can keep the
    Agent's cover-based choice.
    """
    width = int(canvas.get("width") or 0)
    height = int(canvas.get("height") or 0)
    if width <= 0 or height <= 0:
        raise SeriesError("invalid_canvas", "系列画布尺寸无效", 422)
    clauses = " | ".join(_logo_clauses(prompt)).lower()
    position = "bottom-right"
    position_found = False
    for word, value in _POSITION_WORDS:
        if word in clauses:
            position = value
            position_found = True
            break
    size = "medium"
    size_found = False
    if _SMALL_SIZE.search(clauses):
        size = "small"
        size_found = True
    elif _LARGE_SIZE.search(clauses):
        size = "large"
        size_found = True
    box_width, box_height = _logo_box_size(canvas, size)
    margin = max(8, int(round(min(width, height) * 0.045)))
    x, y = _place_logo_box(canvas, box_width, box_height, position, margin)
    return {
        "x": x, "y": y, "width": box_width, "height": box_height,
        "position": position, "size": size, "fit": "contain",
        "source": "user" if (position_found or size_found) else "default",
        "position_specified": position_found,
        "size_specified": size_found,
        "margin": margin,
    }


def merge_logo_box(
    canvas: dict[str, Any], prompt: str, agent_box: dict[str, Any] | None, *,
    apply_size_hint: bool = True,
) -> dict[str, Any]:
    """Combine the user's explicit Logo hints with the Agent's cover-based box.

    Only the hints the user actually stated are overridden; the other property keeps the
    Agent's choice, so "Logo 放左下角" does not also shrink the Logo and "Logo 小一点" does
    not move it.
    """
    hint = resolve_logo_box(canvas, prompt)
    if hint["source"] != "user":
        return agent_box or hint
    if not isinstance(agent_box, dict):
        return hint
    box = {
        "x": int(agent_box.get("x") or 0),
        "y": int(agent_box.get("y") or 0),
        "width": int(agent_box.get("width") or hint["width"]),
        "height": int(agent_box.get("height") or hint["height"]),
        "position": str(agent_box.get("position") or "agent"),
        "size": str(agent_box.get("size") or "agent"),
        "fit": "contain",
        "source": "user",
    }
    if apply_size_hint and hint.get("size_specified"):
        box["width"], box["height"] = hint["width"], hint["height"]
        box["size"] = hint["size"]
    if hint.get("position_specified"):
        margin = max(8, int(round(min(int(canvas["width"]), int(canvas["height"])) * 0.045)))
        box["x"], box["y"] = _place_logo_box(
            canvas, box["width"], box["height"], hint["position"], margin
        )
        box["position"] = hint["position"]
    box["width"] = max(16, min(int(canvas["width"]), box["width"]))
    box["height"] = max(8, min(int(canvas["height"]), box["height"]))
    box["x"] = max(0, min(int(canvas["width"]) - box["width"], box["x"]))
    box["y"] = max(0, min(int(canvas["height"]) - box["height"], box["y"]))
    return box


def _logo_options(canvas: dict[str, Any], prompt: str, artwork: dict[str, Any]) -> list[dict[str, Any]]:
    hint = resolve_logo_box(canvas, prompt)
    canvas_width, canvas_height = int(canvas["width"]), int(canvas["height"])
    image_width, image_height = artwork["image_size"]
    left, top, right, bottom = artwork["ink_bounds"]
    ink_extent = max(right - left, bottom - top)
    min_scale = artwork["min_visible_px"] / ink_extent
    fit_scale = min(canvas_width / image_width, canvas_height / image_height)
    if fit_scale < min_scale:
        raise SeriesError("series_logo_unreadable", "官方 Logo 在当前画布上无法达到最小可见尺寸", 422)
    requested_scale = hint["width"] / max(image_width, image_height) if hint["size_specified"] else 0
    scale = min(fit_scale, max(requested_scale, (artwork["min_visible_px"] + 2) / ink_extent))
    width = min(canvas_width, math.ceil(image_width * scale))
    height = min(canvas_height, math.ceil(image_height * scale))
    padding = max(1, round(min(canvas_width, canvas_height) * 0.01))
    positions = (hint["position"],) if hint["position_specified"] else (
        "top-left", "top-right", "bottom-left", "bottom-right",
    )
    options = []
    for position in positions:
        x, y = _place_logo_box(canvas, width, height, position, hint["margin"])
        # Project the visible artwork, including room for resampling at its edges.
        safe_x = max(0, x + math.floor(left * width / image_width) - padding)
        safe_y = max(0, y + math.floor(top * height / image_height) - padding)
        safe_right = min(canvas_width, x + math.ceil(right * width / image_width) + padding)
        safe_bottom = min(canvas_height, y + math.ceil(bottom * height / image_height) + padding)
        options.append({
            "position": position,
            "logo_box": {"x": x, "y": y, "width": width, "height": height},
            "safe_area": {"x": safe_x, "y": safe_y,
                          "width": safe_right - safe_x, "height": safe_bottom - safe_y},
        })
    return options


def composite_logo(base_png: bytes, logo_png: bytes, box: dict[str, Any], canvas: dict[str, Any]) -> bytes:
    """Overlay the official logo file inside the stored box, preserving its ratio.

    The official file keeps its own pixels, placement and scale. When those pixels would
    be hard to read against the design behind them (white artwork on cream, or a dark
    mark on a dark photo), a compact neutral plate is drawn behind the mark only.
    """
    try:
        with Image.open(io.BytesIO(base_png)) as source:
            base = ImageOps.exif_transpose(source).convert("RGBA")
            if base.size != (int(canvas["width"]), int(canvas["height"])):
                base = ImageOps.fit(
                    base,
                    (int(canvas["width"]), int(canvas["height"])),
                    method=Image.Resampling.LANCZOS,
                )
        with Image.open(io.BytesIO(logo_png)) as source:
            logo = ImageOps.exif_transpose(source).convert("RGBA")
            target_width = max(1, int(box.get("width") or logo.width))
            target_height = max(1, int(box.get("height") or logo.height))
            minimum = int(box.get("min_visible_px") or 0)
            if minimum:
                logo = ImageOps.contain(logo, (target_width, target_height), Image.Resampling.LANCZOS)
                _check_logo_size(logo, minimum)
            else:
                logo.thumbnail((target_width, target_height), Image.Resampling.LANCZOS)
            x = max(0, int(box.get("x") or 0) + (target_width - logo.width) // 2)
            y = max(0, int(box.get("y") or 0) + (target_height - logo.height) // 2)
            _add_contrast_plate(base, logo, x, y, box)
            if minimum:
                _check_logo_contrast(base.crop((x, y, x + logo.width, y + logo.height)), logo)
            base.alpha_composite(logo, (x, y))
        buffer = io.BytesIO()
        base.convert("RGB").save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()
    except SeriesError:
        raise
    except Exception as exc:  # noqa: BLE001 - provider/logo bytes are untrusted
        raise SeriesError("invalid_logo_render", "Logo 叠加失败", 422) from exc


def _logo_ink_box(logo: Image.Image) -> tuple[int, int, int, int] | None:
    return logo.getchannel("A").point(lambda alpha: 255 if alpha >= _LOGO_INK_ALPHA else 0).getbbox()


def _check_logo_size(logo: Image.Image, minimum: int) -> None:
    bounds = _logo_ink_box(logo)
    if not bounds or max(bounds[2] - bounds[0], bounds[3] - bounds[1]) < minimum:
        raise SeriesError("series_logo_unreadable", "官方 Logo 的实际可见尺寸过小，无法交付", 502)


def _check_logo_contrast(backdrop: Image.Image, logo: Image.Image) -> None:
    alpha = np.asarray(logo.getchannel("A"), dtype=np.float64) / 255
    ink = alpha >= _LOGO_INK_ALPHA / 255
    weights = alpha[ink] ** 2
    background = _relative_luminance(np.asarray(backdrop.convert("RGB"))[ink])
    foreground = _relative_luminance(np.asarray(Image.alpha_composite(backdrop, logo).convert("RGB"))[ink])
    contrast = (np.maximum(background, foreground) + 0.05) / (np.minimum(background, foreground) + 0.05)
    if np.average(contrast >= _LOGO_CONTRAST_TARGET, weights=weights) < 0.75:
        raise SeriesError("series_logo_unreadable", "官方 Logo 与背景对比不足，无法交付", 502)


def _relative_luminance(rgb: Any) -> Any:
    """Linear relative luminance of sRGB values in 0-255 (scalar or per-pixel rows)."""
    channels = np.asarray(rgb, dtype=np.float64) / 255.0
    linear = np.where(
        channels <= 0.03928, channels / 12.92, ((channels + 0.055) / 1.055) ** 2.4
    )
    return linear @ np.array([0.2126, 0.7152, 0.0722])


def _contrast_ratio(first: float, second: float) -> float:
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


def _contrast_plate_choice(
    mark_luminance: float, backdrop_rgb: Any, current_ratio: float
) -> tuple[tuple[int, int, int], float] | None:
    """Pick the faintest neutral plate that lifts the mark above the legibility threshold.

    Opacity is tried first so a barely-there plate wins over a stronger one, and the two
    neutral tones are compared at that opacity. Returns ``None`` when no plate beats the
    design as it already is, keeping opaque and mid-tone artwork untouched.
    """
    backdrop = np.asarray(backdrop_rgb, dtype=np.float64)
    best: tuple[float, tuple[int, int, int], float] | None = None
    for opacity in _PLATE_OPACITIES:
        chosen: tuple[float, tuple[int, int, int]] | None = None
        for color in (_PLATE_DARK, _PLATE_LIGHT):
            blended = backdrop * (1.0 - opacity) + np.asarray(color, dtype=np.float64) * opacity
            ratio = _contrast_ratio(mark_luminance, float(_relative_luminance(blended)))
            if chosen is None or ratio > chosen[0]:
                chosen = (ratio, color)
            if best is None or ratio > best[0]:
                best = (ratio, color, opacity)
        if chosen[0] >= _LOGO_CONTRAST_TARGET:
            return chosen[1], opacity
    if best is None or best[0] <= current_ratio:
        return None
    return best[1], best[2]


def _plate_tile(
    width: int, height: int, radius: int, color: tuple[int, int, int], opacity: float
) -> Image.Image:
    """A rounded neutral plate, drawn at 4x so the corners stay smooth when shrunk."""
    scale = 4
    tile = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle(
        (0, 0, width * scale - 1, height * scale - 1),
        radius=max(1, radius * scale),
        fill=(*color, int(round(255 * opacity))),
    )
    return tile.resize((width, height), Image.Resampling.LANCZOS)


def _add_contrast_plate(
    base: Image.Image,
    logo: Image.Image,
    x: int,
    y: int,
    box: dict[str, Any],
) -> None:
    """Draw a backing plate behind a mark that would otherwise disappear.

    Only alpha-weighted visible ink is measured, so a transparent Logo file is judged by
    what actually shows. The plate hugs the mark inside the stored box, is clipped to the
    canvas and never moves or recolours the official pixels.
    """
    page_width, page_height = base.size
    left, top = max(0, x), max(0, y)
    right, bottom = min(page_width, x + logo.width), min(page_height, y + logo.height)
    if right <= left or bottom <= top:
        return
    visible = logo.crop((left - x, top - y, right - x, bottom - y))
    alpha = np.asarray(visible.getchannel("A"), dtype=np.float64) / 255.0
    ink = alpha >= _LOGO_INK_ALPHA / 255.0
    if not ink.any():
        return
    weights = alpha[ink]
    mark = (_relative_luminance(np.asarray(visible.convert("RGB"))[ink]) * weights).sum()
    mark_luminance = float(mark / weights.sum())
    backdrop_pixels = np.asarray(base.crop((left, top, right, bottom)).convert("RGB"))[ink]
    backdrop_luminance = float(
        (_relative_luminance(backdrop_pixels) * weights).sum() / weights.sum()
    )
    current_ratio = _contrast_ratio(mark_luminance, backdrop_luminance)
    if current_ratio >= _LOGO_CONTRAST_TARGET:
        return
    backdrop_rgb = (backdrop_pixels * weights[:, None]).sum(axis=0) / weights.sum()
    choice = _contrast_plate_choice(mark_luminance, backdrop_rgb, current_ratio)
    if choice is None:
        return
    color, opacity = choice
    ink_box = _logo_ink_box(visible)
    if ink_box is None:
        return
    pad = max(2, int(round(min(ink_box[2] - ink_box[0], ink_box[3] - ink_box[1]) * 0.08)))
    box_left = int(box.get("x") or 0)
    box_top = int(box.get("y") or 0)
    plate = (
        max(0, box_left, left + ink_box[0] - pad),
        max(0, box_top, top + ink_box[1] - pad),
        min(page_width, box_left + int(box.get("width") or logo.width), left + ink_box[2] + pad),
        min(page_height, box_top + int(box.get("height") or logo.height), top + ink_box[3] + pad),
    )
    plate_width, plate_height = plate[2] - plate[0], plate[3] - plate[1]
    if plate_width <= 0 or plate_height <= 0:
        return
    radius = max(3, int(round(min(plate_width, plate_height) * 0.22)))
    base.alpha_composite(
        _plate_tile(plate_width, plate_height, radius, color, opacity), (plate[0], plate[1])
    )


def validate_png(
    data: bytes, canvas: dict[str, Any], *, status_code: int = 502
) -> tuple[int, int]:
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            size = image.size
    except Exception as exc:  # noqa: BLE001
        raise SeriesError("invalid_png", "成图无法解码", status_code) from exc
    if size != (int(canvas["width"]), int(canvas["height"])):
        raise SeriesError("invalid_canvas_size", "成图尺寸与系列画布不一致", status_code)
    return size


def series_logo(
    db: Any, task: dict[str, Any], version: dict[str, Any] | None = None
) -> tuple[str, dict[str, Any] | None]:
    """Resolve the page's official Logo file and fixed box for one version.

    The group snapshot wins over a stale task copy so every page of a group, including
    edits and replacements, composites the same box with the same original file.
    """
    series = task.get("series") or {}
    group = db.whole_series_groups.find_one({"_id": series.get("group_id")}) if series.get("group_id") else None
    candidates = [
        (version or {}).get("series_logo_box"),
        (group or {}).get("logo_box"),
        series.get("logo_box"),
    ]
    logo_box = next((item for item in candidates if isinstance(item, dict) and item), None)
    logo_asset_id = str((group or {}).get("logo_asset_id", series.get("logo_asset_id")) or "").strip()
    return logo_asset_id, logo_box


def finalize_series_outputs(
    db: Any,
    settings: Settings | Any,
    store: Any,
    *,
    task: dict[str, Any],
    version: int,
    outputs: list[dict[str, Any]],
    canvas: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Turn saved no-Logo results into the page deliverables by adding the official Logo.

    Returns ``(output_asset_id, outputs, version_fields)``. With Logo enabled the raw
    result stays available as a non-selectable base and the composited PNG is the only
    selectable output, so display, single downloads, group ZIPs and finalizations all
    read the same pixels. With Logo disabled the raw result is the deliverable.
    """
    if not outputs:
        raise SeriesError("output_missing", "没有可交付的成图", 502)
    width, height = int(canvas["width"]), int(canvas["height"])
    logo_asset_id, logo_box = series_logo(db, task)
    if not logo_asset_id:
        for output in outputs:
            output["selectable"] = True
            output.setdefault("label", "成品")
        return str(outputs[0]["asset_id"]), outputs, {
            "series_base_asset_id": str(outputs[0]["asset_id"]),
            "series_logo_box": None,
        }
    if not logo_box:
        raise SeriesError("logo_box_missing", "启用 Logo 时缺少本组 Logo 位置", 409)
    logo = db.assets.find_one({"_id": logo_asset_id})
    if logo is None:
        raise SeriesError("logo_missing", "官方 Logo 不存在", 422)
    logo_bytes = store.get_bytes(str(logo.get("prepared_path") or logo.get("path")))
    composed_outputs: list[dict[str, Any]] = []
    for output in outputs:
        base_asset = db.assets.find_one({"_id": str(output["asset_id"])}) or {}
        base_key = str(base_asset.get("prepared_path") or base_asset.get("path") or "")
        if not base_key:
            raise SeriesError("asset_missing", "成图文件不存在", 404)
        base_bytes = store.get_bytes(base_key)
        composed = composite_logo(base_bytes, logo_bytes, logo_box, canvas)
        validate_png(composed, canvas)
        saved_final = _save_output_asset(
            db, settings, store, task_id=str(task["_id"]), version=version, data=composed,
            role="series_final", target_size=(width, height),
        )
        composed_outputs.append({**output, "selectable": False, "label": "无 Logo 底图"})
        composed_outputs.append({
            **{key: value for key, value in output.items() if key not in {"id", "role", "label"}},
            "id": "series_final" if len(outputs) == 1 else f"series_final_{output.get('id')}",
            "role": "series_final",
            "label": "成品",
            "asset_id": str(saved_final["asset_id"]),
            "width": width,
            "height": height,
            "selectable": True,
        })
    final = next(item for item in composed_outputs if item.get("selectable"))
    return str(final["asset_id"]), composed_outputs, {
        "series_base_asset_id": str(outputs[0]["asset_id"]),
        "series_logo_box": dict(logo_box),
        "series_logo_asset_id": logo_asset_id,
    }


# --------------------------------------------------------------------------------------
# Workspace mode
# --------------------------------------------------------------------------------------

def _mode_id(db: Any, workspace_id: str) -> str:
    identity = _merchant_id(db) + "\0" + str(workspace_id)
    return "wmode_" + hashlib.sha256(identity.encode()).hexdigest()


def get_workspace_mode(db: Any, workspace_id: str, settings: Settings | Any) -> dict[str, Any]:
    workspace_id = str(workspace_id or "").strip()
    if not workspace_id:
        raise SeriesError("invalid_workspace", "工作区 ID 不能为空", 422)
    record = db.whole_workspace_modes.find_one({"workspace_id": workspace_id})
    if record is None:
        # Historical workspaces predate the mode field; their stored tasks decide.
        if db.whole_series_groups.find_one({"workspace_id": workspace_id}) is not None:
            mode = "series"
        elif db.whole_tasks.find_one(
            {"$or": [
                {"input.generation_workspace_id": workspace_id},
                {"input.candidate_batch_id": workspace_id,
                 "input.generation_workspace_id": None},
                {"_id": workspace_id, "input.generation_workspace_id": None,
                 "input.candidate_batch_id": None},
            ]}
        ) is not None:
            mode = "single"
        else:
            mode = None
        return {"workspace_id": workspace_id, "mode": mode or "single",
                "locked": False, "series_enabled": enabled(settings)}
    return {"workspace_id": workspace_id, "mode": record["mode"],
            "locked": True, "series_enabled": enabled(settings)}


def claim_workspace_mode(db: Any, workspace_id: str, mode: str) -> dict[str, Any]:
    workspace_id = str(workspace_id or "").strip()
    if not workspace_id:
        raise SeriesError("invalid_workspace", "工作区 ID 不能为空", 422)
    if mode not in SERIES_MODES:
        raise SeriesError("invalid_mode", "海报模式无效", 422)
    identity = _mode_id(db, workspace_id)
    existing = db.whole_workspace_modes.find_one({"_id": identity})
    if existing is not None:
        if existing.get("mode") != mode:
            raise SeriesError("workspace_mode_locked",
                              "当前任务已锁定为另一种海报模式，请新建海报", 409)
        return existing
    if mode == "series" and db.whole_tasks.find_one(
        {"$or": [
            {"input.generation_workspace_id": workspace_id},
            {"input.candidate_batch_id": workspace_id,
             "input.generation_workspace_id": None},
            {"_id": workspace_id, "input.generation_workspace_id": None,
             "input.candidate_batch_id": None},
        ]},
        {"_id": 1},
    ) is not None:
        raise SeriesError("workspace_mode_locked",
                          "当前任务已按单张海报创建，请新建海报使用系列图", 409)
    now = utcnow()
    record = {
        "_id": identity,
        "workspace_id": workspace_id,
        "mode": mode,
        "created_at": now,
        "updated_at": now,
    }
    try:
        db.whole_workspace_modes.insert_one(record)
    except DuplicateKeyError:
        existing = db.whole_workspace_modes.find_one({"_id": identity})
        if existing is None or existing.get("mode") != mode:
            raise SeriesError("workspace_mode_locked",
                              "当前任务已锁定为另一种海报模式，请新建海报", 409) from None
        return existing
    return record


# --------------------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------------------

def _mention_indexes(prompt: str) -> list[int]:
    indexes: list[int] = []
    for match in re.finditer(r"@?(?:图片|图)\s*(\d+)", str(prompt or "")):
        value = int(match.group(1))
        if value >= 1 and value not in indexes:
            indexes.append(value)
    return indexes


def plan_group_assets(
    asset_ids: list[str],
    page_count: int,
    prompt: str,
    assets: dict[str, dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Split the uploaded pool across the pages of one group.

    Explicit ``@图片N`` mentions become the pool the group works from: the cover gets
    the first mentioned image and interiors share the rest. Without mentions the cover
    takes the first upload and interiors cycle through the remaining ones. Unused
    uploads stay in the task's asset pool for manual replacement.
    """
    ordered = [str(item) for item in asset_ids if str(item)]
    mentioned = [
        ordered[index - 1] for index in _mention_indexes(prompt)
        if index - 1 < len(ordered)
    ]
    pool = list(dict.fromkeys(mentioned or ordered))
    if not pool:
        return [[] for _ in range(page_count)]
    cover = [pool[0]]
    remaining = pool[1:] or pool
    pages: list[list[dict[str, Any]]] = [cover]
    for index in range(1, page_count):
        asset_id = remaining[(index - 1) % len(remaining)]
        pages.append([asset_id])
    return [
        [
            {
                "asset_id": asset_id,
                "target": str((assets.get(asset_id) or {}).get("original_name") or ""),
            }
            for asset_id in dict.fromkeys(page_assets)
        ]
        for page_assets in pages
    ]


def _page_prompt(user_prompt: str, role: str, order: int, page_count: int) -> str:
    if role == "cover":
        suffix = (f"本页是这组系列图的封面（第 1 页，共 {page_count} 页）。"
                  "标题和主视觉用于统领整组，可以只使用一张主图。")
    else:
        suffix = (f"本页是这组系列图的第 {order + 1} 页介绍页（共 {page_count} 页）。"
                  "承接封面的配色、字形风格和取景方式，介绍本页自己的内容。")
    return f"{user_prompt.strip()}\n\n{suffix}"


def _group_pages_payload(payload: PosterSeriesRunCreate) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    order = 0
    for model_group in payload.image_groups:
        for _ in range(int(model_group.group_count)):
            groups.append({"image_model": model_group.image_model, "order": order})
            order += 1
    return groups


def _run_out(db: Any, run: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(run["_id"]),
        "workspace_id": run["workspace_id"],
        "round": int(run.get("round") or 1),
        "status": run.get("status"),
        "prompt": run.get("prompt"),
        "pages_per_group": None if run.get("workflow") else int(run.get("pages_per_group") or 2),
        "workflow": run.get("workflow"),
        "error": run.get("error"),
        "auto_generate_interiors": bool(run.get("auto_generate_interiors")),
        "canvas": run.get("canvas"),
        "created_at": run.get("created_at"),
    }


def _page_asset(db: Any, asset_id: str | None) -> dict[str, Any] | None:
    if not asset_id:
        return None
    return db.assets.find_one({"_id": asset_id})


def _page_version(db: Any, page: dict[str, Any]) -> dict[str, Any] | None:
    task_id = page.get("whole_task_id")
    if not task_id:
        return None
    task = db.whole_tasks.find_one({"_id": task_id})
    if task is None:
        return None
    version_id = task.get("current_version_id")
    if not version_id:
        return None
    return db.whole_versions.find_one({"_id": version_id, "task_id": task_id})


def _page_output(version: dict[str, Any] | None, current_output_id: str | None) -> dict[str, Any] | None:
    if not version:
        return None
    outputs = _version_outputs(version)
    if current_output_id:
        selected = next((item for item in outputs if item.get("id") == current_output_id), None)
        if selected:
            return selected
    return next((item for item in outputs if item.get("selectable")), None)


def _current_content_refs(
    db: Any,
    task: dict[str, Any] | None,
    version: dict[str, Any] | None,
    page: dict[str, Any],
) -> list[dict[str, Any]]:
    refs = (version or {}).get("series_content_refs")
    if refs is None and (version or {}).get("parent_version_id") and task is not None:
        parent = db.whole_versions.find_one(
            {"_id": version["parent_version_id"], "task_id": task["_id"]}
        )
        refs = (parent or {}).get("series_content_refs")
    return [
        {"asset_id": str(item.get("asset_id")), "target": str(item.get("target") or "")}
        if isinstance(item, dict) else {"asset_id": str(item), "target": ""}
        for item in (refs if refs is not None else page.get("content_refs") or [])
        if (item.get("asset_id") if isinstance(item, dict) else item)
    ]


def page_out(db: Any, page: dict[str, Any], *, merchant_id: str = "") -> dict[str, Any]:
    from app.api_paths import asset_thumb_url, asset_url, whole_download_url

    task = db.whole_tasks.find_one({"_id": page["whole_task_id"]}) if page.get("whole_task_id") else None
    version = _page_version(db, page)
    output = _page_output(version, (task or {}).get("current_output_id"))
    asset_id = (output or {}).get("asset_id") if output else None
    owner = str((task or {}).get("merchant_id") or merchant_id or "")
    version_number = int((version or {}).get("version") or 0)
    status = str((task or {}).get("status") or page.get("status") or "planned")
    error = None
    if task:
        if (task.get("generation") or {}).get("error"):
            error = str(task["generation"]["error"])
        elif isinstance(task.get("error"), dict):
            error = task["error"].get("message")
    return {
        "page_id": page["_id"],
        "group_id": page["group_id"],
        "run_id": page["run_id"],
        "order": int(page.get("order") or 0),
        "role": page.get("role"),
        "status": status,
        "whole_task_id": page.get("whole_task_id"),
        "version_id": str(version["_id"]) if version else None,
        "version": version_number,
        "output_id": (output or {}).get("id"),
        "asset_id": asset_id,
        "preview_url": asset_url(owner, asset_id, version=version_number) if asset_id else None,
        "thumb_url": asset_thumb_url(owner, asset_id) if asset_id else None,
        "download_url": (
            whole_download_url(owner, str(task["_id"]), str(version["_id"]),
                               (output or {}).get("id"))
            if task and version and asset_id else None
        ),
        "error": error,
        # Defaults follow the currently selected version so a replacement or edit never
        # shows the previous photo in the replacement panel.
        "content_refs": _current_content_refs(db, task, version, page),
        "style_reference_ids": list(
            (version or {}).get("series_style_reference_ids")
            or page.get("style_reference_ids") or []
        ),
        "cover_reference": page.get("cover_reference"),
        "logo_box": (version or {}).get("series_logo_box") or page.get("logo_box"),
        "created_at": page.get("created_at"),
        "updated_at": (task or page).get("updated_at"),
    }


def _group_content_pool(
    db: Any, group: dict[str, Any], *, merchant_id: str
) -> list[dict[str, Any]]:
    """List every uploaded content input so the workbench can offer unused photos.

    Style references and the official Logo are excluded: they are not content photos and
    must never appear in an original-photo replacement list.
    """
    from app.api_paths import asset_thumb_url, asset_url

    excluded = {str(value) for value in group.get("style_reference_ids") or []}
    if group.get("logo_asset_id"):
        excluded.add(str(group["logo_asset_id"]))
    ids = [
        str(value) for value in group.get("content_pool") or []
        if str(value) not in excluded
    ]
    if not ids:
        return []
    owner = merchant_id or str(group.get("merchant_id") or "")
    documents = {
        str(item["_id"]): item for item in db.assets.find({"_id": {"$in": ids}})
    }
    pool: list[dict[str, Any]] = []
    for asset_id in ids:
        asset = documents.get(asset_id)
        if asset is None:
            continue
        pool.append({
            "asset_id": asset_id,
            "name": str(asset.get("original_name") or ""),
            "preview_url": asset_url(owner, asset_id),
            "thumb_url": asset_thumb_url(owner, asset_id),
        })
    return pool


def _group_out(db: Any, group: dict[str, Any], pages: list[dict[str, Any]], *, merchant_id: str) -> dict[str, Any]:
    page_items = [page_out(db, page, merchant_id=merchant_id) for page in pages]
    cover = next((item for item in page_items if item["role"] == "cover"), None)
    succeeded = sum(1 for item in page_items if item["status"] == "succeeded")
    failed = sum(1 for item in page_items if item["status"] == "failed")
    running = sum(1 for item in page_items if item["status"] in {"queued", "running"})
    page_count = int(group.get("page_count", 2))
    complete = page_count > 0 and succeeded == page_count
    return {
        "id": group["_id"],
        "run_id": group["run_id"],
        "workspace_id": group["workspace_id"],
        "order": int(group.get("order") or 0),
        "image_model": group.get("image_model"),
        "page_count": page_count,
        "status": group.get("status") or ("complete" if complete else "running" if running else "failed" if failed else "pending"),
        "error": group.get("error"),
        "cover_page_id": group.get("cover_page_id"),
        "cover_ready": bool(cover and cover["status"] == "succeeded" and cover["asset_id"]),
        "interiors_started_at": group.get("interiors_started_at"),
        "auto_generate_interiors": bool(group.get("auto_generate_interiors")),
        "complete": complete,
        "can_download": complete and all(item["asset_id"] for item in page_items),
        "succeeded_count": succeeded,
        "failed_count": failed,
        "running_count": running,
        "logo_box": group.get("logo_box"),
        "logo_asset_id": group.get("logo_asset_id"),
        "canvas": group.get("canvas"),
        # The complete uploaded content pool (minus style references/Logo) so the
        # replacement panel can offer photos that no page used yet.
        "content_pool": _group_content_pool(db, group, merchant_id=merchant_id),
        "style_reference_ids": list(group.get("style_reference_ids") or []),
        "pages": page_items,
        "created_at": group.get("created_at"),
    }


def get_workspace_series(
    db: Any,
    workspace_id: str,
    settings: Settings | Any,
    *,
    merchant_id: str = "",
) -> dict[str, Any]:
    mode = get_workspace_mode(db, workspace_id, settings)
    groups: list[dict[str, Any]] = []
    if enabled(settings) and mode["mode"] == "series":
        for group in db.whole_series_groups.find({"workspace_id": workspace_id}).sort("order", 1):
            pages = list(db.whole_series_pages.find({"group_id": group["_id"]}).sort("order", 1))
            groups.append(_group_out(db, group, pages, merchant_id=merchant_id))
    runs = [
        _run_out(db, run)
        for run in db.whole_series_runs.find({"workspace_id": workspace_id}).sort("created_at", 1)
    ]
    return {
        "workspace_id": workspace_id,
        "mode": mode["mode"],
        "locked": mode["locked"],
        "series_enabled": enabled(settings),
        "runs": runs,
        "groups": groups,
    }


# --------------------------------------------------------------------------------------
# Run creation
# --------------------------------------------------------------------------------------

def _series_task_payload(
    payload: PosterSeriesRunCreate,
    *,
    workspace_id: str,
    model: str,
    page_prompt: str,
    page_assets: list[dict[str, Any]],
    title: str | None,
) -> WholeImageTaskCreate:
    values: dict[str, Any] = dict(
        image_model=model,
        prompt=page_prompt,
        prompt_optimized=payload.prompt_optimized,
        product_asset_ids=[],
        reference_asset_ids=[item["asset_id"] for item in page_assets],
        brand_id=payload.brand_id,
        brand_mode=payload.brand_mode,
        logo_asset_id=payload.logo_asset_id,
        style_reference_asset_ids=payload.style_reference_asset_ids,
        candidate_count=1,
        candidate_batch_id=None,
        generation_workspace_id=workspace_id,
        workspace_title=title,
        candidate_index=0,
        platform_id=payload.platform_id,
        placement_id=payload.placement_id,
        include_store_address=payload.include_store_address,
        include_store_phone=payload.include_store_phone,
        store_place_id=payload.store_place_id,
    )
    if payload.ratio is not None:
        # Only an explicit ratio may override the platform/placement canvas. A series
        # submission that omits it must inherit the selected platform preset exactly
        # like a single-poster submission does.
        values["ratio"] = payload.ratio
    return WholeImageTaskCreate(**values)


def create_run(
    db: Any,
    settings: Settings | Any,
    workspace_id: str,
    payload: PosterSeriesRunCreate,
    *,
    build_task: Callable[[WholeImageTaskCreate], dict[str, Any]],
    launch: Callable[[str], None],
    merchant_id: str = "",
) -> dict[str, Any]:
    require_enabled(settings)
    workspace_id = str(workspace_id or "").strip()
    if not workspace_id:
        raise SeriesError("invalid_workspace", "工作区 ID 不能为空", 422)
    existing = db.whole_series_runs.find_one(
        {"workspace_id": workspace_id, "submit_id": payload.submit_id}
    )
    if existing is not None:
        return get_workspace_series(db, workspace_id, settings, merchant_id=merchant_id)

    reference_ids = list(payload.reference_asset_ids or payload.product_asset_ids or [])
    assets = {str(item["_id"]): item for item in db.assets.find({"_id": {"$in": reference_ids}})}
    pages_per_group = resolve_page_count(payload.prompt, payload.pages_per_group, settings)
    # Validate and build every cover before writing anything. An invalid submission
    # must never lock the workspace mode or leave a half-created round behind.
    prepared: list[dict[str, Any]] = []
    for item in _group_pages_payload(payload):
        content_plan = plan_group_assets(reference_ids, pages_per_group, payload.prompt, assets)
        cover_prompt = _page_prompt(payload.prompt, "cover", 0, pages_per_group)
        cover_payload = _series_task_payload(
            payload, workspace_id=workspace_id, model=item["image_model"],
            page_prompt=cover_prompt, page_assets=content_plan[0],
            title=payload.workspace_title if not prepared else None,
        )
        cover_doc = build_task(cover_payload)
        canvas = dict(cover_doc.get("canvas") or {})
        logo_box = (
            resolve_logo_box(canvas, payload.prompt)
            if payload.logo_asset_id else None
        )
        if logo_box is not None and logo_box.get("source") != "user":
            # Position/size were not specified: the group plan Agent decides the box
            # from the actual cover design instead of a hardwired default.
            logo_box = None
        prepared.append({
            "item": item,
            "content_plan": content_plan,
            "cover_doc": cover_doc,
            "canvas": canvas,
            "logo_box": logo_box,
        })

    mode_existed = db.whole_workspace_modes.find_one({"workspace_id": workspace_id}) is not None
    claim_workspace_mode(db, workspace_id, "series")
    run_id = f"wsrun_{uuid.uuid4().hex}"
    now = utcnow()
    round_number = int(db.whole_series_runs.count_documents({"workspace_id": workspace_id})) + 1
    run_doc = {
        "_id": run_id,
        "workspace_id": workspace_id,
        "submit_id": payload.submit_id,
        "round": round_number,
        "prompt": payload.prompt,
        "pages_per_group": pages_per_group,
        "auto_generate_interiors": bool(payload.auto_generate_interiors),
        "image_groups": [
            {"image_model": item.image_model, "group_count": int(item.group_count)}
            for item in payload.image_groups
        ],
        "status": "running",
        "created_at": now,
        "updated_at": now,
    }
    queued_by_page: list[list[str]] = [[] for _ in range(pages_per_group)]
    try:
        db.whole_series_runs.insert_one(run_doc)
        for entry in prepared:
            item = entry["item"]
            content_plan = entry["content_plan"]
            cover_doc = entry["cover_doc"]
            canvas = entry["canvas"]
            logo_box = entry["logo_box"]
            group_id = f"wsgrp_{uuid.uuid4().hex}"
            group_doc = {
                "_id": group_id,
                "run_id": run_id,
                "workspace_id": workspace_id,
                "order": int(item["order"]),
                "image_model": item["image_model"],
                "page_count": pages_per_group,
                "prompt": payload.prompt,
                "canvas": canvas,
                "logo_asset_id": payload.logo_asset_id,
                "logo_box": logo_box,
                "logo_box_source": "user" if logo_box else "agent",
                "content_pool": list(reference_ids),
                "style_reference_ids": list(payload.style_reference_asset_ids),
                "auto_generate_interiors": bool(payload.auto_generate_interiors),
                "status": "running",
                "cover_page_id": None,
                "interiors_started_at": now if payload.auto_generate_interiors else None,
                "agent_asset_urls": {},
                "task_template": _task_template(cover_doc),
                "created_at": now,
                "updated_at": now,
            }
            db.whole_series_groups.insert_one(group_doc)
            page_docs: list[dict[str, Any]] = []
            for order in range(pages_per_group):
                role = "cover" if order == 0 else "detail"
                page_doc = {
                    "_id": f"wspage_{uuid.uuid4().hex}",
                    "run_id": run_id,
                    "group_id": group_id,
                    "workspace_id": workspace_id,
                    "order": order,
                    "role": role,
                    "status": "planned",
                    "whole_task_id": None,
                    "content_refs": content_plan[order],
                    "style_reference_ids": list(payload.style_reference_asset_ids),
                    "cover_reference": None,
                    "logo_box": logo_box,
                    "created_at": now,
                    "updated_at": now,
                }
                db.whole_series_pages.insert_one(page_doc)
                page_docs.append(page_doc)
            cover_page = page_docs[0]
            db.whole_series_groups.update_one(
                {"_id": group_id}, {"$set": {"cover_page_id": cover_page["_id"]}}
            )
            cover_doc["series"] = {
                "run_id": run_id,
                "group_id": group_id,
                "page_id": cover_page["_id"],
                "order": 0,
                "role": "cover",
                "page_count": pages_per_group,
                "user_prompt": payload.prompt,
                "content_refs": content_plan[0],
                "style_reference_ids": list(payload.style_reference_asset_ids),
                "cover_reference": None,
                "logo_box": logo_box,
                "logo_asset_id": payload.logo_asset_id,
                "auto_generate_interiors": bool(payload.auto_generate_interiors),
                "status": "queued",
            }
            db.whole_tasks.insert_one(cover_doc)
            task_id = str(cover_doc["_id"])
            db.whole_series_pages.update_one(
                {"_id": cover_page["_id"]},
                {"$set": {"whole_task_id": task_id, "status": "queued", "updated_at": now}},
            )
            queued_by_page[0].append(task_id)
            if payload.auto_generate_interiors:
                for page_doc in page_docs[1:]:
                    page_order = int(page_doc["order"])
                    detail_task = _new_page_task_doc(
                        db, group_doc, page_doc, role="detail",
                        prompt=_page_prompt(payload.prompt, "detail", page_order, pages_per_group)
                        + "\n\n封面和本页按同一个系列规划并行生成。请使用 group_plan 中的共用视觉方案延续封面风格。",
                    )
                    db.whole_tasks.insert_one(detail_task)
                    _update_page(db, page_doc["_id"], {"whole_task_id": detail_task["_id"], "status": "queued"})
                    queued_by_page[page_order].append(str(detail_task["_id"]))
        for task_ids in queued_by_page:
            for task_id in task_ids:
                launch(task_id)
    except Exception as exc:
        logger.exception("poster series round failed to start")
        # Roll back the partial round and, if this submission created the mode record,
        # remove it so a retry is not blocked by a failed attempt.
        db.whole_tasks.delete_many({"series.run_id": run_id})
        db.whole_series_pages.delete_many({"run_id": run_id})
        db.whole_series_groups.delete_many({"run_id": run_id})
        db.whole_series_runs.delete_one({"_id": run_id})
        if not mode_existed and db.whole_series_groups.find_one({"workspace_id": workspace_id}) is None:
            db.whole_workspace_modes.delete_one({"workspace_id": workspace_id})
        raise
    db.whole_series_runs.update_one({"_id": run_id}, {"$set": {"updated_at": utcnow()}})
    return get_workspace_series(db, workspace_id, settings, merchant_id=merchant_id)


def _page_system_context(task: dict[str, Any], *, role: str) -> str:
    brand = task.get("brand") if isinstance(task.get("brand"), dict) else {}
    parts = [_DETAIL_SYSTEM_PROMPT if role == "detail" else _COVER_SYSTEM_PROMPT]
    if brand.get("name"):
        parts.append(brand_rules.brand_context(brand))
        style_context = _brand_style_context(brand)
        if style_context:
            parts.append(style_context)
    rules_context = brand_rules.rules_context(task.get("brand_rules"))
    if rules_context:
        parts.append(rules_context)
    if task.get("merchant_context"):
        parts.append(str(task["merchant_context"]))
    platform_context = platform_presets.prompt_from_task(
        task.get("platform"), task.get("placement")
    )
    if platform_context:
        parts.append(platform_context)
    if not rules_context:
        parts.append(_FOOD_RULE)
    return "\n\n".join(part for part in parts if part)


def _brand_style_context(brand: dict[str, Any]) -> str:
    # Kept local so the series prompt does not import the whole single-poster module.
    guide = str(brand.get("layout_guide") or "").strip()
    background = str(brand.get("background_guide") or "").strip()
    return "\n".join(
        part for part in (
            f"版式要求：{guide}" if guide else "",
            f"背景要求：{background}" if background else "",
        ) if part
    )


def _load_asset_ref(db: Any, store: Any, asset_id: str, label: str) -> ImageRef:
    asset = db.assets.find_one({"_id": asset_id})
    if asset is None:
        raise SeriesError("asset_missing", f"{label}不存在", 422)
    try:
        return image_ref_from_asset(db, store, str(asset_id))
    except Exception as exc:  # noqa: BLE001
        raise SeriesError("asset_missing", str(exc), 422) from exc


def _update_page(db: Any, page_id: str, values: dict[str, Any]) -> None:
    db.whole_series_pages.update_one(
        {"_id": page_id}, {"$set": {**values, "updated_at": utcnow()}}
    )


def _extract_agent_payload(output: str) -> dict[str, Any]:
    text = str(output or "").strip()
    if not text:
        raise SeriesError("agent_contract_invalid", "系列图 Agent 未返回结果", 502)
    candidates: list[str] = []
    try:
        candidates.append(text)
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    for block in re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S):
        candidates.append(block)
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise SeriesError("agent_contract_invalid", "系列图 Agent 返回结果不是约定 JSON", 502)


_TRANSIENT_UPLOAD_HINTS = (
    "connect", "connection", "reset", "timeout", "timed out", "temporar",
    "remote protocol", "broken pipe", "unreachable", "network", "econn",
    "getaddrinfo", "ssl", "10053", "10054", "10060", "10061", "10065",
)


def _is_transient_upload_error(exc: BaseException) -> bool:
    """Only network-ish upload failures are worth retrying.

    Missing assets, bad credentials or an empty file must fail immediately; retrying them
    would just delay the user's error. Generation triggers are never retried here.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (ConnectionError, TimeoutError)):
            return True
        if isinstance(current, OSError):
            if getattr(current, "errno", None) in {101, 104, 110, 111, 113}:
                return True
            if getattr(current, "winerror", None) in {10053, 10054, 10060, 10061, 10065}:
                return True
        current = current.__cause__ or current.__context__
    text = str(exc).lower()
    return any(hint in text for hint in _TRANSIENT_UPLOAD_HINTS)


def _upload_asset_url(
    db: Any, settings: Settings | Any, store: Any, client: Any, asset_id: str,
    *, use_original: bool = False,
) -> str:
    """Upload one asset with a small bounded retry for transient network errors."""
    from app.services.agent_image import asset_public_url

    attempts = max(1, int(getattr(settings, "poster_series_asset_upload_attempts", 3) or 1))
    delay = max(0.0, float(getattr(settings, "poster_series_asset_upload_retry_seconds", 1.0) or 0.0))
    expires = int(getattr(settings, "cos_presign_expiry_seconds", 3600))
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return asset_public_url(db, settings, store, client, asset_id, expires=expires, use_original=use_original)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt >= attempts or not _is_transient_upload_error(exc):
                raise SeriesError(
                    "asset_upload_failed", f"素材上传 Agent 失败：{exc}", 502,
                ) from exc
            logger.warning(
                "series asset upload retry %s/%s for %s: %s", attempt, attempts, asset_id, exc,
            )
            if delay:
                time.sleep(delay * attempt)
    raise SeriesError("asset_upload_failed", f"素材上传 Agent 失败：{last}", 502)


def _agent_asset_urls(
    db: Any, settings: Settings | Any, store: Any, client: Any, group: dict[str, Any],
    asset_ids: list[str],
) -> dict[str, str]:
    cache = dict(group.get("agent_asset_urls") or {})
    missing = [asset_id for asset_id in asset_ids if not cache.get(asset_id)]
    for asset_id in missing:
        cache[asset_id] = _upload_asset_url(db, settings, store, client, asset_id)
        # Persist each success right away: a later failure then only re-uploads the rest.
        db.whole_series_groups.update_one(
            {"_id": group["_id"]}, {"$set": {"agent_asset_urls": cache}}
        )
        group["agent_asset_urls"] = dict(cache)
    return cache


def _validate_agent_logo_box(box: Any, canvas: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(box, dict):
        raise SeriesError("agent_logo_missing", "启用 Logo 时 Agent 未给出封面 Logo 位置", 502)
    try:
        x, y = int(box.get("x")), int(box.get("y"))
        width, height = int(box.get("width")), int(box.get("height"))
    except (TypeError, ValueError) as exc:
        raise SeriesError("agent_logo_invalid", "Agent 返回的 Logo 位置无效", 502) from exc
    canvas_width, canvas_height = int(canvas["width"]), int(canvas["height"])
    if width < 16 or height < 8 or x < 0 or y < 0 or x + width > canvas_width or y + height > canvas_height:
        raise SeriesError("agent_logo_invalid", "Agent 返回的 Logo 框超出画布或过小", 502)
    return {
        "x": x, "y": y, "width": width, "height": height,
        "position": str(box.get("position") or "agent"),
        "size": str(box.get("size") or "agent"),
        "fit": "contain",
        "source": "agent",
    }


def _logo_artwork(
    db: Any, store: Any, logo_id: str, canvas: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe the official mark for layout without adding it to the model's photo pool."""
    logo = db.assets.find_one({"_id": logo_id})
    if logo is None:
        raise SeriesError("logo_missing", "官方 Logo 不存在", 422)
    data = store.get_bytes(str(logo.get("prepared_path") or logo.get("path")))
    with Image.open(io.BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source).convert("RGBA")
        result: dict[str, Any] = {"aspect_ratio": round(image.width / image.height, 2)}
        if canvas is not None:
            bounds = _logo_ink_box(image)
            if bounds is None:
                raise SeriesError("series_logo_unreadable", "官方 Logo 文件没有可见内容", 422)
            minimum = (min(int(canvas["width"]), int(canvas["height"])) * 14 + 99) // 100
            ink_extent = max(bounds[2] - bounds[0], bounds[3] - bounds[1])
            # Account for transparent margins and resampling at the visible edges.
            scale = (minimum + 2) / ink_extent
            result.update(min_visible_px=minimum, min_box_width=math.ceil(image.width * scale),
                          min_box_height=math.ceil(image.height * scale),
                          image_size=list(image.size), ink_bounds=list(bounds))
        image.thumbnail((128, 128), Image.Resampling.LANCZOS)
    pixels = np.asarray(image)
    ink = pixels[:, :, 3] >= _LOGO_INK_ALPHA
    if ink.any():
        colors = pixels[ink]
        rgb = np.average(colors[:, :3], axis=0, weights=colors[:, 3]).round().astype(int)
        result["mark_color"] = "#" + "".join(f"{channel:02X}" for channel in rgb)
    return result


def _ensure_group_plan(
    db: Any, settings: Settings | Any, store: Any, client: Any,
    group: dict[str, Any], pages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Ask the Agent for one group outline shared by every page call.

    The outline fixes each page's role, title, intent and preferred references, and the
    Logo box when the user did not specify one. It is stored before any page renders so
    later interiors continue the same plan instead of planning independently.
    """
    existing = group.get("plan")
    if isinstance(existing, dict) and existing.get("pages"):
        return existing
    agent_id = str(getattr(settings, "core_ai_series_agent_id", "") or "").strip()
    if not agent_id:
        raise SeriesError("series_agent_not_configured",
                          "系列图 Agent 未配置，请设置 CORE_AI_SERIES_AGENT_ID", 503)
    pool_ids = [str(item) for item in group.get("content_pool") or []]
    style_ids = [str(item) for item in group.get("style_reference_ids") or []]
    asset_ids = list(dict.fromkeys([*pool_ids, *style_ids]))
    urls = _agent_asset_urls(db, settings, store, client, group, asset_ids)
    documents = {str(item["_id"]): item for item in db.assets.find({"_id": {"$in": asset_ids}})}
    content_pool = [
        {
            "asset_id": asset_id,
            "name": str((documents.get(asset_id) or {}).get("original_name") or "图片"),
            "url": urls[asset_id],
            "role": "style_reference" if asset_id in style_ids else "content",
        }
        for asset_id in asset_ids
    ]
    user_box = group.get("logo_box") if group.get("logo_box_source") == "user" else None
    logo_id = str(group.get("logo_asset_id") or "")
    artwork = _logo_artwork(db, store, logo_id) if logo_id else None
    payload = {
        "operation": "poster-series-plan",
        "group": {
            "group_id": group["_id"],
            "model": group.get("image_model"),
            "canvas": group.get("canvas"),
            "logo": {
                "enabled": bool(logo_id),
                "user_box": user_box,
                "artwork": artwork,
            },
        },
        "user_prompt": group.get("prompt") or "",
        "pages": [
            {"page_id": page["_id"], "order": int(page.get("order") or 0), "role": page.get("role")}
            for page in pages
        ],
        "content_pool": content_pool,
        "output_contract": {
            "format": "json",
            "fields": {
                "group_id": "same as request",
                "pages": [{
                    "page_id": "from request",
                    "role": "cover|detail",
                    "title": "planned on-image title",
                    "intent": "what this page introduces",
                    "content_refs": [{"asset_id": "from content_pool", "target": "string"}],
                    "style_reference_ids": ["asset_id from content_pool"],
                }],
                "visual_direction": "Shared palette, typography, framing and decorative language for every page",
                "logo_box": {"x": 0, "y": 0, "width": 0, "height": 0},
                "logo_treatment": "When enabled, compact contrasting background integrated in the layout around logo_box",
            },
        },
    }
    timeout_seconds = int(getattr(settings, "core_ai_series_agent_timeout_seconds", 1800) or 1800)
    plan_run_id = str(group.get("plan_run_id") or "").strip()
    if plan_run_id:
        # A restart continues the already-started planning run instead of paying again.
        result = client.wait_agent_detail(plan_run_id, timeout_seconds=timeout_seconds)
    else:
        with poster_costs.track(db, group.get("workspace_id"), "plan"):
            plan_run_id = client.trigger_agent(
                agent_id, json.dumps(payload, ensure_ascii=False), timeout_seconds=30
            )
        db.whole_series_groups.update_one(
            {"_id": group["_id"]},
            {"$set": {"plan_run_id": plan_run_id, "updated_at": utcnow()}},
        )
        result = client.wait_agent_detail(plan_run_id, timeout_seconds=timeout_seconds)
    parsed = _extract_agent_payload(result.output)
    if parsed.get("error"):
        raise SeriesError("agent_plan_failed", str(parsed["error"]), 502)
    if str(parsed.get("group_id") or "") != str(group["_id"]):
        raise SeriesError("agent_contract_invalid", "Agent 规划的组 ID 不一致", 502)
    requested = {page["_id"]: page for page in pages}
    planned: list[dict[str, Any]] = []
    for item in parsed.get("pages") or []:
        page_id = str(item.get("page_id") or "")
        page = requested.get(page_id)
        if page is None:
            raise SeriesError("agent_contract_invalid", "Agent 规划了不存在的页", 502)
        refs = []
        for ref in item.get("content_refs") or []:
            asset_id = str(ref.get("asset_id") or "") if isinstance(ref, dict) else ""
            if asset_id not in pool_ids:
                raise SeriesError("agent_reference_invalid", "Agent 规划引用了素材池以外的内容", 502)
            refs.append({"asset_id": asset_id, "target": str(ref.get("target") or "")})
        styles = [str(value) for value in item.get("style_reference_ids") or []
                  if str(value) in pool_ids or str(value) in style_ids]
        planned.append({
            "page_id": page_id,
            "role": str(item.get("role") or page.get("role")),
            "title": str(item.get("title") or ""),
            "intent": str(item.get("intent") or ""),
            "content_refs": refs,
            "style_reference_ids": styles,
        })
    if set(item["page_id"] for item in planned) != set(requested):
        raise SeriesError("agent_contract_invalid", "Agent 规划缺少页面", 502)
    plan = {"group_id": group["_id"], "pages": planned, "agent_run_id": result.run_id}
    if isinstance(parsed.get("visual_direction"), str):
        plan["visual_direction"] = parsed["visual_direction"].strip()[:2000]
    update: dict[str, Any] = {"plan": plan, "plan_run_id": result.run_id, "updated_at": utcnow()}
    if logo_id:
        treatment = parsed.get("logo_treatment")
        if isinstance(treatment, str) and treatment.strip():
            plan["logo_treatment"] = treatment.strip()[:500]
        canvas = group.get("canvas") or {}
        agent_box = None
        if parsed.get("logo_box") is not None:
            agent_box = _validate_agent_logo_box(parsed.get("logo_box"), canvas)
        elif not user_box:
            raise SeriesError(
                "agent_logo_missing", "启用 Logo 时 Agent 未给出封面 Logo 位置", 502
            )
        merged = merge_logo_box(canvas, group.get("prompt") or "", agent_box)
        update["logo_box"] = merged
        update["logo_box_source"] = {
            "user": "user", "agent": "agent", "default": "default",
        }.get(str(merged.get("source")), "agent")
    db.whole_series_groups.update_one({"_id": group["_id"]}, {"$set": update})
    return plan


def _span_tool_name(span: dict[str, Any]) -> str:
    attributes = span.get("attributes") if isinstance(span.get("attributes"), dict) else {}
    return str(
        span.get("tool") or span.get("tool_name") or span.get("name")
        or attributes.get("gen_ai.tool.name") or attributes.get("tool.name")
        or attributes.get("tool") or ""
    )


def _span_succeeded(span: dict[str, Any]) -> bool:
    attributes = span.get("attributes") if isinstance(span.get("attributes"), dict) else {}
    status = span.get("status", span.get("status_code"))
    text = str(status if status is not None else attributes.get("status") or "").upper()
    if text in {"ERROR", "FAILED", "FAILURE", "CANCELLED", "STATUS_CODE_ERROR"}:
        return False
    if attributes.get("error.type") or span.get("error"):
        return False
    return True


def _span_model(span: dict[str, Any]) -> str:
    attributes = span.get("attributes") if isinstance(span.get("attributes"), dict) else {}
    for key in ("model", "gen_ai.request.model", "gen_ai.response.model", "image.model"):
        value = span.get(key) or attributes.get(key)
        if value:
            return str(value)
    return ""


def _trace_id(detail: dict[str, Any]) -> str:
    for key in ("trace_id", "traceId"):
        if detail.get(key):
            return str(detail[key])
    trace = detail.get("trace")
    if isinstance(trace, dict):
        for key in ("id", "trace_id", "traceId"):
            if trace.get(key):
                return str(trace[key])
    return ""


def _verify_agent_trace(
    client: Any, run_id: str, requested_model: str, expected_urls: list[str],
    expected_prompt: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Prove the native image call from persisted observability data.

    Structured arguments are validated strictly (model, every expected reference URL and
    the exact prompt). The platform sometimes persists run rows with null name/arguments
    while the trace span still records a successful native ``generate_image`` call;
    that case is accepted as *reported*, with the span and the caveat stored as evidence,
    because a completed run with a real decodable PNG is a real deliverable. Absent both
    signals the page still fails.
    """
    try:
        detail = client.get_agent_run(run_id)
    except Exception as exc:  # noqa: BLE001
        raise SeriesError("agent_trace_missing", f"无法读取 Agent 运行记录：{exc}", 502) from exc
    calls: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            name = str(value.get("tool") or value.get("tool_name") or value.get("name") or "")
            args = value.get("arguments") or value.get("args") or value.get("parameters") or value.get("input")
            if name == "generate_image" and isinstance(args, dict):
                input_images = args.get("input_images")
                if isinstance(input_images, str):
                    try:
                        input_images = json.loads(input_images)
                    except ValueError:
                        input_images = []
                urls = []
                for image in input_images or []:
                    if isinstance(image, dict):
                        url = image.get("url") or image.get("sandbox_path") or image.get("media_id")
                        if url:
                            urls.append(str(url))
                calls.append({
                    "tool": "generate_image",
                    "model": str(args.get("model") or ""),
                    "quality": str(args.get("quality") or ""),
                    "prompt": str(args.get("prompt") or ""),
                    "input_image_urls": urls,
                })
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(detail)
    if calls:
        expected = set(expected_urls)
        if not expected:
            raise SeriesError("agent_trace_mismatch", "没有可用于核对的内容素材", 502)
        matched = [
            call for call in calls
            if gateway_model_name(call["model"]) == requested_model
            and expected.issubset(set(call["input_image_urls"]))
            and call["prompt"].strip() == expected_prompt.strip()
        ]
        if not matched:
            raise SeriesError(
                "agent_trace_mismatch",
                "Agent 运行记录中的生图模型、提示词或全部输入素材与请求不一致",
                502,
            )
        accepted = dict(matched[0])
        accepted["verification"] = {
            "level": "args_verified",
            "run_id": run_id,
            "caveat": None,
        }
        return calls, accepted

    trace_id = _trace_id(detail)
    spans: list[dict[str, Any]] = []
    if trace_id:
        reader = getattr(client, "get_agent_trace_spans", None)
        if callable(reader):
            try:
                spans = reader(trace_id)
            except Exception:  # noqa: BLE001 - observability outage must not fake evidence
                logger.warning("trace spans unavailable for run %s", run_id, exc_info=True)
                spans = []
    native = [
        span for span in spans
        if _span_tool_name(span) == "generate_image" and _span_succeeded(span)
    ]
    if not native:
        raise SeriesError(
            "agent_trace_missing",
            "无法核对 Agent 的生图调用：运行记录没有参数，也没有成功的 generate_image 调用记录",
            502,
        )
    mismatched = [
        span for span in native
        if _span_model(span) and gateway_model_name(_span_model(span)) != requested_model
    ]
    if len(mismatched) == len(native):
        raise SeriesError(
            "agent_trace_mismatch", "生图调用记录中的模型与请求不一致", 502
        )
    span = next(item for item in native if item not in mismatched)
    reported = {
        "tool": "generate_image",
        "model": _span_model(span) or requested_model,
        "quality": "",
        "prompt": "",
        "input_image_urls": [],
        "verification": {
            "level": "tool_span_reported",
            "run_id": run_id,
            "trace_id": trace_id,
            "span_id": str(span.get("id") or span.get("span_id") or ""),
            "span_name": str(span.get("name") or ""),
            "caveat": (
                "运行记录中的工具参数未公开，仅核对到成功的原生 generate_image 调用；"
                "模型、素材角色与封面引用由 Agent 结构化回执和实际 PNG 校验。"
            ),
        },
    }
    return calls, reported


def _agent_page_input(
    db: Any, settings: Settings | Any, store: Any, client: Any,
    task: dict[str, Any], group: dict[str, Any],
) -> dict[str, Any]:
    series = task.get("series") or {}
    role = str(series.get("role") or "cover")
    pool_ids = [
        str(item) for item in group.get("content_pool")
        or (task.get("input") or {}).get("reference_asset_ids") or []
    ]
    style_ids = [str(item) for item in group.get("style_reference_ids") or series.get("style_reference_ids") or []]
    cover_reference = series.get("cover_reference") or {}
    cover_id = str(cover_reference.get("asset_id") or "").strip()
    asset_ids = list(dict.fromkeys([*pool_ids, *style_ids, *([cover_id] if cover_id else [])]))
    urls = _agent_asset_urls(db, settings, store, client, group, asset_ids)
    documents = {str(item["_id"]): item for item in db.assets.find({"_id": {"$in": asset_ids}})}
    assets = [
        {
            "asset_id": asset_id,
            "name": str((documents.get(asset_id) or {}).get("original_name") or "图片"),
            "url": urls[asset_id],
            "role": (
                "style_reference" if asset_id in style_ids
                else "cover_reference" if asset_id == cover_id
                else "content"
            ),
        }
        for asset_id in asset_ids
    ]
    return {
        "operation": "poster-series-page",
        "page": {
            "page_id": series.get("page_id"),
            "order": int(series.get("order") or 0),
            "role": role,
            "page_count": int(series.get("page_count") or 2),
        },
        "group": {
            "group_id": group["_id"],
            "model": group.get("image_model"),
            "canvas": group.get("canvas"),
            "logo": {
                "enabled": bool(group.get("logo_asset_id")),
                "box": group.get("logo_box"),
                "placement_hint": group.get("prompt"),
                "treatment": (group.get("plan") or {}).get("logo_treatment"),
            },
        },
        "user_prompt": group.get("prompt") or "",
        "page_prompt": (task.get("input") or {}).get("prompt") or "",
        "requested_model": group.get("image_model"),
        "content_pool": assets,
        "group_plan": group.get("plan"),
        "page_plan": next(
            (item for item in (group.get("plan") or {}).get("pages") or []
             if str(item.get("page_id")) == str(series.get("page_id"))),
            None,
        ),
        "planned_content_refs": list(series.get("content_refs") or []),
        "cover": (
            {
                "asset_id": cover_id,
                "version_id": cover_reference.get("version_id"),
                "url": urls.get(cover_id),
            }
            if role == "detail" and cover_id else None
        ),
        "output_contract": {
            "format": "json",
            "fields": {
                "page_id": "string",
                "role": "cover|detail",
                "model": "the exact model actually sent to generate_image",
                "prompt": "the exact prompt actually sent to generate_image",
                "content_refs": [{"asset_id": "from content_pool", "target": "string"}],
                "style_reference_ids": ["asset_id from content_pool used only as style"],
                "cover_asset_id": "cover.asset_id when cover is supplied, otherwise null",
            },
            "artifact": "submit exactly one final PNG for this page as page.png",
        },
    }


def _validate_agent_payload(
    task: dict[str, Any], group: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    series = task.get("series") or {}
    role = str(series.get("role") or "cover")
    pool = {
        str(item) for item in group.get("content_pool")
        or (task.get("input") or {}).get("reference_asset_ids") or []
    }
    style_pool = {str(item) for item in group.get("style_reference_ids") or series.get("style_reference_ids") or []}
    allowed = pool | style_pool
    if str(payload.get("page_id") or "") != str(series.get("page_id") or ""):
        raise SeriesError("agent_contract_invalid", "Agent 返回的页 ID 与请求不一致", 502)
    if str(payload.get("role") or role) != role:
        raise SeriesError("agent_contract_invalid", "Agent 返回的页角色与请求不一致", 502)
    requested_model = gateway_model_name(group.get("image_model"))
    actual_model = gateway_model_name(str(payload.get("model") or ""))
    if actual_model != requested_model:
        # A model mismatch would silently break the four-model contract.
        raise SeriesError(
            "agent_model_mismatch",
            f"Agent 实际使用模型 {payload.get('model')}，请求为 {group.get('image_model')}",
            502,
        )
    content_refs: list[dict[str, Any]] = []
    for item in payload.get("content_refs") or []:
        asset_id = str(item.get("asset_id") or "") if isinstance(item, dict) else ""
        if asset_id not in pool:
            raise SeriesError("agent_reference_invalid", "Agent 引用了素材池以外的内容素材", 502)
        content_refs.append({
            "asset_id": asset_id,
            "target": str(item.get("target") or ""),
        })
    if not content_refs:
        raise SeriesError("agent_reference_invalid", "Agent 未返回内容素材引用", 502)
    style_ids = [str(item) for item in payload.get("style_reference_ids") or [] if str(item) in allowed]
    cover_id = str((series.get("cover_reference") or {}).get("asset_id") or "")
    if role == "detail":
        reported_cover = str(payload.get("cover_asset_id") or "")
        if reported_cover != cover_id:
            raise SeriesError(
                "agent_cover_mismatch", "Agent 实际使用的封面与锁定的封面不一致", 502
            )
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise SeriesError("agent_contract_invalid", "Agent 未记录实际生图提示词", 502)
    return {
        "content_refs": content_refs,
        "style_reference_ids": style_ids,
        "cover_asset_id": cover_id,
        "prompt": prompt,
        "model": requested_model,
    }


def run_round_generation(db: Any, settings: Settings | Any, store: Any, client: Any,
                         task_ids: list[str]) -> None:
    if not task_ids:
        return
    generate = partial(run_page_generation, db, settings, store, client)
    with ThreadPoolExecutor(max_workers=min(len(task_ids), _ROUND_WORKERS)) as pool:
        for _ in pool.map(generate, task_ids):
            pass


def run_page_generation(
    db: Any,
    settings: Settings | Any,
    store: Any,
    client: Any,
    task_id: str,
) -> None:
    """Generate one series page by running the configured native Core AI Agent.

    The Agent plans the page, chooses its references, calls the platform
    ``generate_image`` tool with the requested model, and submits one PNG. The app
    records the Agent run id, actual model, actual prompt and returned references.
    There is no direct-gateway fallback: an unconfigured Agent fails the page.
    """
    from pymongo import ReturnDocument

    claimed = db.whole_tasks.find_one_and_update(
        {"_id": task_id, "generation.status": "queued"},
        {"$set": {
            "status": "running",
            "stage": "planning",
            "progress": 10,
            "generation": {"status": "running", "error": None, "started_at": utcnow()},
            "provider.status": "running",
            "series.status": "running",
            "updated_at": utcnow(),
        }},
        return_document=ReturnDocument.AFTER,
    )
    if claimed is None:
        return
    series = claimed.get("series") or {}
    page_id = str(series.get("page_id") or "")
    group_id = str(series.get("group_id") or "")
    saved_assets: list[str] = []
    try:
        agent_id = str(getattr(settings, "core_ai_series_agent_id", "") or "").strip()
        if not agent_id:
            raise SeriesError(
                "series_agent_not_configured",
                "系列图 Agent 未配置，请设置 CORE_AI_SERIES_AGENT_ID",
                503,
            )
        # Pages of a round share one stored plan. Only planning holds the lock;
        # model calls are independent as soon as that plan is published.
        with _PLAN_LOCKS[hash(group_id) % len(_PLAN_LOCKS)]:
            group = _group(db, group_id)
            if not (group.get("plan") or {}).get("pages"):
                pages = list(db.whole_series_pages.find({"group_id": group_id}).sort("order", 1))
                _ensure_group_plan(db, settings, store, client, group, pages)
                group = _group(db, group_id)
        input_data = claimed.get("input") or {}
        canvas = dict(claimed.get("canvas") or {})
        role = str(series.get("role") or "cover")
        agent_payload = _agent_page_input(db, settings, store, client, claimed, group)
        db.whole_tasks.update_one(
            {"_id": task_id},
            {"$set": {
                "stage": "generating",
                "progress": 30,
                "series.agent_status": "running",
                "series.agent_input": {
                    "agent_id": agent_id,
                    "page_id": series.get("page_id"),
                    "model": group.get("image_model"),
                    "asset_ids": [item["asset_id"] for item in agent_payload["content_pool"]],
                },
                "updated_at": utcnow(),
            }},
        )
        timeout_seconds = int(getattr(settings, "core_ai_series_agent_timeout_seconds", 1800) or 1800)
        agent_run_id = str(series.get("agent_run_id") or "").strip()
        if agent_run_id:
            # PS-11: a restart resumes the saved run; it must not trigger a new paid run.
            result = client.wait_agent_detail(agent_run_id, timeout_seconds=timeout_seconds)
        else:
            with poster_costs.track_task(db, claimed, "generate"):
                agent_run_id = client.trigger_agent(
                    agent_id, json.dumps(agent_payload, ensure_ascii=False), timeout_seconds=30
                )
            db.whole_tasks.update_one(
                {"_id": task_id},
                {"$set": {"series.agent_run_id": agent_run_id,
                          "generation.agent_run_id": agent_run_id,
                          "series.agent_status": "running",
                          "updated_at": utcnow()}},
            )
            result = client.wait_agent_detail(agent_run_id, timeout_seconds=timeout_seconds)
        parsed = _validate_agent_payload(claimed, group, _extract_agent_payload(result.output))
        selected_assets = {
            item["asset_id"] for item in parsed["content_refs"]
        } | set(parsed["style_reference_ids"])
        if parsed["cover_asset_id"]:
            selected_assets.add(parsed["cover_asset_id"])
        expected_urls = [
            str(item["url"]) for item in agent_payload["content_pool"]
            if item["asset_id"] in selected_assets
        ]
        trace_calls, accepted_call = _verify_agent_trace(
            client, result.run_id, parsed["model"], expected_urls, parsed["prompt"]
        )
        images = client.download_agent_images(result)
        if not images:
            raise SeriesError("agent_image_missing", "Agent 未交付页面 PNG", 502)
        data = images[0].data
        content_ids = [item["asset_id"] for item in parsed["content_refs"]]
        style_ids = parsed["style_reference_ids"]
        cover_asset_id = parsed["cover_asset_id"] if role == "detail" else ""
        model_input = {
            "provider": "core-ai",
            "channel": "agent",
            "endpoint": "agent:generate_image",
            "model": parsed["model"],
            "prompt": parsed["prompt"],
            "parameters": {
                "agent_id": agent_id,
                "run_id": result.run_id,
                "canvas": canvas,
                "n": 1,
                "output_format": "png",
            },
            "images": [
                *[
                    {
                        "asset_id": asset_id,
                        "name": str((db.assets.find_one({"_id": asset_id}) or {}).get("original_name") or "图片"),
                        "role": "content",
                    }
                    for asset_id in content_ids
                ],
                *([
                    {
                        "asset_id": cover_asset_id,
                        "name": str((db.assets.find_one({"_id": cover_asset_id}) or {}).get("original_name") or "封面参考"),
                        "role": "cover_reference",
                    }
                ] if cover_asset_id else []),
                *[
                    {"asset_id": asset_id, "name": "风格参考", "role": "style_reference"}
                    for asset_id in style_ids
                ],
            ],
            "agent": {
                "run_id": result.run_id,
                "output_url": images[0].source_url,
                "file_name": images[0].file_name,
                "trace_verified": (
                    (accepted_call.get("verification") or {}).get("level") == "args_verified"
                ),
                "trace_verification": accepted_call.get("verification"),
                "accepted_generate_image": accepted_call,
                "generate_image_calls": trace_calls,
            },
        }
        db.whole_tasks.update_one(
            {"_id": task_id},
            {"$set": {
                "model_input_attempt": model_input,
                "series.agent_run_id": result.run_id,
                "series.agent_model": parsed["model"],
                "series.agent_trace": trace_calls,
                "series.agent_trace_verification": accepted_call.get("verification"),
                "series.content_refs": parsed["content_refs"],
                "series.style_reference_ids": style_ids,
                "updated_at": utcnow(),
            }},
        )
        saved_base = _save_output_asset(
            db, settings, store, task_id=task_id, version=1, data=data,
            role="series_base", target_size=(int(canvas["width"]), int(canvas["height"])),
        )
        base_asset_id = str(saved_base["asset_id"])
        saved_assets.append(base_asset_id)
        final_asset_id = base_asset_id
        logo_asset_id = str(series.get("logo_asset_id") or "").strip()
        logo_box = group.get("logo_box") or series.get("logo_box")
        if logo_asset_id and not isinstance(logo_box, dict):
            raise SeriesError("agent_logo_missing", "启用 Logo 时缺少本组 Logo 位置", 502)
        if logo_asset_id and isinstance(logo_box, dict):
            logo = db.assets.find_one({"_id": logo_asset_id})
            if logo is None:
                raise SeriesError("logo_missing", "官方 Logo 不存在", 422)
            base_bytes = store.get_bytes(str(saved_base["path"]))
            logo_bytes = store.get_bytes(str(logo.get("prepared_path") or logo.get("path")))
            composed = composite_logo(base_bytes, logo_bytes, logo_box, canvas)
            validate_png(composed, canvas)
            saved_final = _save_output_asset(
                db, settings, store, task_id=task_id, version=1, data=composed,
                role="series_final", target_size=(int(canvas["width"]), int(canvas["height"])),
            )
            final_asset_id = str(saved_final["asset_id"])
            saved_assets.append(final_asset_id)
        outputs = [{
            "id": "series_base",
            "channel": "agent",
            "role": "series_base",
            "label": "无 Logo 底图",
            "asset_id": base_asset_id,
            "width": int(canvas["width"]),
            "height": int(canvas["height"]),
            "selectable": final_asset_id == base_asset_id,
            "model_input": model_input,
            "created_at": utcnow(),
        }]
        if final_asset_id != base_asset_id:
            outputs.append({
                "id": "series_final",
                "channel": "agent",
                "role": "series_final",
                "label": "成品",
                "asset_id": final_asset_id,
                "width": int(canvas["width"]),
                "height": int(canvas["height"]),
                "selectable": True,
                "model_input": model_input,
                "created_at": utcnow(),
            })
        document = _version_document(
            task_id=task_id,
            version=1,
            parent_version_id=None,
            output_asset_id=final_asset_id,
            instruction=parsed["prompt"],
            operation="series_generate",
            width=int(canvas["width"]),
            height=int(canvas["height"]),
            model_input=model_input,
            outputs=outputs,
        )
        document["series_base_asset_id"] = base_asset_id
        document["series_content_refs"] = [
            {"asset_id": str(item["asset_id"]), "target": str(item.get("target") or "")}
            for item in parsed["content_refs"]
        ]
        document["series_style_reference_ids"] = style_ids
        document["series_cover_reference"] = series.get("cover_reference")
        document["series_logo_box"] = logo_box
        document["series_role"] = role
        document["series_page_id"] = page_id
        document["series_agent_run_id"] = result.run_id
        _commit_version(db, store, task_id=task_id, version_doc=document, generation=True)
        _append_message(
            db, task_id, role="assistant",
            content=f"系列第 {int(series.get('order') or 0) + 1} 页已生成。",
            version_id=str(document["_id"]), version=1,
        )
        db.whole_tasks.update_one(
            {"_id": task_id},
            {"$set": {
                "series.status": "succeeded",
                "series.version_id": str(document["_id"]),
                "series.base_asset_id": base_asset_id,
                "series.final_asset_id": final_asset_id,
                "series.completed_at": utcnow(),
                "updated_at": utcnow(),
            }},
        )
        _update_page(db, page_id, {
            "status": "succeeded",
            "version_id": str(document["_id"]),
            "output_id": "series_final" if final_asset_id != base_asset_id else "series_base",
            "content_refs": parsed["content_refs"],
            "style_reference_ids": style_ids,
            "agent_run_id": result.run_id,
            "error": None,
        })
        _refresh_group(db, group_id)
        if role == "cover":
            try:
                on_cover_complete(db, settings, store, client, group_id, document)
            except Exception:  # noqa: BLE001 - auto-start must never fail the cover
                logger.exception("auto interior start failed for group %s", group_id)
    except Exception as exc:  # noqa: BLE001 - persist one actionable failure
        logger.exception("series page generation failed for %s", task_id)
        for asset_id in saved_assets:
            try:
                _delete_asset(db, store, asset_id)
            except Exception:  # noqa: BLE001
                pass
        message = _error_text(exc)
        db.whole_tasks.update_one(
            {"_id": task_id},
            {"$set": {
                "status": "failed", "stage": "failed", "progress": 0,
                "error": {"code": "generation_failed", "message": message},
                "generation.status": "failed", "generation.error": message,
                "provider.status": "failed", "series.status": "failed",
                "series.agent_status": "failed",
                "updated_at": utcnow(),
            }},
        )
        if page_id:
            _update_page(db, page_id, {"status": "failed", "error": message})
        if group_id:
            _refresh_group(db, group_id)


def attach_series_refs(db: Any, task_id: str, version_doc: dict[str, Any]) -> None:
    """Stamp one authoritative content-reference list on a series version.

    Called from the shared version commit path for every operation (edit, original
    replacement, HTML save, retry). The current version's list replaces the parent's
    entries for the affected targets, so a replaced photo is not counted twice.
    """
    task = db.whole_tasks.find_one(
        {"_id": task_id}, {"series": 1, "input": 1, "brand": 1}
    ) or {}
    series = task.get("series") or {}
    if not series:
        return
    if version_doc.get("series_content_refs"):
        return
    parent = None
    if version_doc.get("parent_version_id"):
        parent = db.whole_versions.find_one(
            {"_id": version_doc.get("parent_version_id"), "task_id": task_id}
        )
    base = (parent or {}).get("series_content_refs")
    if not base:
        base = series.get("content_refs") or []
    refs = [
        {"asset_id": str(item.get("asset_id") if isinstance(item, dict) else item),
         "target": str(item.get("target") or "") if isinstance(item, dict) else ""}
        for item in base
        if (item.get("asset_id") if isinstance(item, dict) else item)
    ]
    input_data = task.get("input") or {}
    logo_ids = {str(input_data.get("logo_asset_id") or "")}
    logo_ids.update(str(value) for value in (task.get("brand") or {}).get("logo_asset_ids") or [])
    logo_ids.discard("")
    cover_reference = version_doc.get("series_cover_reference") or series.get("cover_reference") or {}
    if cover_reference.get("asset_id"):
        logo_ids.add(str(cover_reference["asset_id"]))
    style_ids = {
        str(value) for value in
        (version_doc.get("series_style_reference_ids") or series.get("style_reference_ids") or [])
    }
    excluded = logo_ids | style_ids
    for replacement in version_doc.get("replacements") or []:
        asset_id = str(replacement.get("asset_id") or "")
        target = str(replacement.get("target") or "")
        if not asset_id or asset_id in excluded:
            continue
        match = next(
            (
                ref for ref in refs
                if target and ref.get("target") and (
                    ref["target"] == target
                    or target in ref["target"]
                    or ref["target"] in target
                )
            ),
            None,
        )
        if match is not None:
            match["asset_id"] = asset_id
            if target:
                match["target"] = target
        elif not any(ref["asset_id"] == asset_id for ref in refs):
            refs.append({"asset_id": asset_id, "target": target})
    for image in (version_doc.get("model_input") or {}).get("images") or []:
        asset_id = str(image.get("asset_id") or "")
        if (
            image.get("role") in {"content", "edit_reference"}
            and asset_id and asset_id not in excluded
            and not any(ref["asset_id"] == asset_id for ref in refs)
        ):
            refs.append({"asset_id": asset_id, "target": ""})
    version_doc["series_content_refs"] = refs


def _refresh_group(db: Any, group_id: str) -> None:
    with _PLAN_LOCKS[hash(group_id) % len(_PLAN_LOCKS)]:
        pages = list(db.whole_series_pages.find({"group_id": group_id}))
        if not pages:
            return
        statuses = {str(page.get("status") or "planned") for page in pages}
        if statuses == {"succeeded"}:
            status = "complete"
        elif statuses & {"queued", "running"}:
            status = "running"
        elif "failed" in statuses:
            status = "failed"
        else:
            status = "pending"
        db.whole_series_groups.update_one(
            {"_id": group_id}, {"$set": {"status": status, "updated_at": utcnow()}}
        )


# --------------------------------------------------------------------------------------
# Interiors and retry
# --------------------------------------------------------------------------------------

def _group(db: Any, group_id: str) -> dict[str, Any]:
    group = db.whole_series_groups.find_one({"_id": group_id})
    if group is None:
        raise SeriesError("group_not_found", "系列组不存在", 404)
    return group


def _page(db: Any, group_id: str, page_id: str) -> dict[str, Any]:
    page = db.whole_series_pages.find_one({"_id": page_id, "group_id": group_id})
    if page is None:
        raise SeriesError("page_not_found", "系列页不存在", 404)
    return page


def _cover_reference(
    db: Any, group: dict[str, Any], version_id: str, output_id: str | None
) -> dict[str, Any]:
    cover_id = str(group.get("cover_page_id") or "")
    cover = db.whole_series_pages.find_one({"_id": cover_id, "group_id": group["_id"]})
    if cover is None or not cover.get("whole_task_id"):
        raise SeriesError("cover_not_ready", "封面尚未生成", 409)
    version = db.whole_versions.find_one({"_id": version_id, "task_id": cover["whole_task_id"]})
    if version is None:
        raise SeriesError("cover_version_not_found", "所选封面版本不属于本组", 409)
    base_asset_id = str(version.get("series_base_asset_id") or "").strip()
    outputs = _version_outputs(version)
    selected = next((item for item in outputs if item.get("id") == output_id), None) if output_id else None
    asset_id = base_asset_id or str((selected or {}).get("asset_id") or version.get("output_asset_id") or "")
    if not asset_id:
        raise SeriesError("cover_output_missing", "所选封面版本没有可用成图", 409)
    return {
        "version_id": str(version["_id"]),
        "version": int(version.get("version") or 1),
        "output_id": (selected or {}).get("id"),
        "asset_id": asset_id,
        "task_id": str(cover["whole_task_id"]),
    }


def _task_template(cover_doc: dict[str, Any]) -> dict[str, Any]:
    """Snapshot the validated platform/brand/canvas fields once per group."""
    return deepcopy({
        "input": cover_doc.get("input") or {},
        "platform": cover_doc.get("platform"),
        "placement": cover_doc.get("placement"),
        "brand": cover_doc.get("brand"),
        "merchant_context": cover_doc.get("merchant_context") or "",
        "brand_rules": cover_doc.get("brand_rules"),
        "canvas": cover_doc.get("canvas") or {},
    })


def _new_page_task_doc(
    db: Any,
    group: dict[str, Any],
    page: dict[str, Any],
    *,
    role: str,
    prompt: str,
    cover_reference: dict[str, Any] | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    template = group.get("task_template")
    if not isinstance(template, dict):
        raise SeriesError("series_template_missing", "系列组缺少生成配置，请重新创建", 409)
    now = utcnow()
    task_id = task_id or f"wtask_{uuid.uuid4().hex}"
    content_ids = [str(item.get("asset_id")) for item in page.get("content_refs") or []]
    input_data = deepcopy(template.get("input") or {})
    input_data.update({
        "image_model": group.get("image_model"),
        "prompt": prompt,
        "prompt_optimized": bool(input_data.get("prompt_optimized")),
        "product_asset_ids": [],
        "reference_asset_ids": content_ids,
        "brand_id": (template.get("brand") or {}).get("id") or input_data.get("brand_id"),
        "logo_asset_id": group.get("logo_asset_id"),
        "style_reference_asset_ids": list(page.get("style_reference_ids") or []),
        "candidate_count": 1,
        "candidate_batch_id": None,
        "generation_workspace_id": group["workspace_id"],
        "candidate_index": 0,
    })
    return {
        "_id": task_id,
        "status": "queued",
        "stage": "pending",
        "progress": 0,
        "input": input_data,
        "platform": deepcopy(template.get("platform")),
        "placement": deepcopy(template.get("placement")),
        "brand": deepcopy(template.get("brand")),
        "merchant_context": template.get("merchant_context") or "",
        "brand_rules": deepcopy(template.get("brand_rules")),
        "canvas": deepcopy(template.get("canvas") or {}),
        "provider": {"name": "core-ai", "model": group.get("image_model"), "status": "queued"},
        "generation": {"status": "queued", "error": None},
        "selected_version_id": None,
        "current_version_id": None,
        "current_version": 0,
        "output_asset_id": None,
        "messages": [{
            "id": f"msg_{uuid.uuid4().hex}",
            "role": "user",
            "content": prompt,
            "created_at": now,
            "version_id": None,
            "version": None,
            "status": "done",
        }],
        "edit": {"status": "idle", "instruction": None, "source_version_id": None, "error": None},
        "error": None,
        "created_at": now,
        "updated_at": now,
        "series": {
            "run_id": group["run_id"],
            "group_id": group["_id"],
            "page_id": page["_id"],
            "order": int(page.get("order") or 0),
            "role": role,
            "page_count": int(group.get("page_count") or 2),
            "user_prompt": group.get("prompt") or "",
            "content_refs": list(page.get("content_refs") or []),
            "style_reference_ids": list(page.get("style_reference_ids") or []),
            "cover_reference": cover_reference,
            "logo_box": group.get("logo_box"),
            "logo_asset_id": group.get("logo_asset_id"),
            "auto_generate_interiors": bool(group.get("auto_generate_interiors")),
            "status": "queued",
        },
    }


def start_interiors(
    db: Any,
    settings: Settings | Any,
    group_id: str,
    *,
    cover_version_id: str,
    cover_output_id: str | None = None,
    launch: Callable[[str], None],
    merchant_id: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Create and launch every not-yet-created interior page of one group.

    Each page is claimed atomically by ``whole_task_id`` so repeated clicks, duplicate
    completion callbacks and a page already in flight all queue exactly one task.
    """
    require_enabled(settings)
    group = _group(db, group_id)
    pages = list(db.whole_series_pages.find({"group_id": group_id}).sort("order", 1))
    cover = next((page for page in pages if page.get("role") == "cover"), None)
    if cover is None:
        raise SeriesError("cover_missing", "系列组缺少封面页", 409)
    if not force and group.get("interiors_started_at"):
        return _group_out(db, group, pages, merchant_id=merchant_id)
    # Capture the chosen cover version once for the whole group. Two concurrent starts
    # with different selected versions must not split pages across cover versions.
    cover_reference = group.get("cover_lock")
    if not isinstance(cover_reference, dict):
        candidate = _cover_reference(db, group, cover_version_id, cover_output_id)
        locked = db.whole_series_groups.find_one_and_update(
            {"_id": group_id, "cover_lock": {"$exists": False}},
            {"$set": {"cover_lock": candidate, "updated_at": utcnow()}},
        )
        if locked is not None:
            cover_reference = candidate
        else:
            cover_reference = (db.whole_series_groups.find_one({"_id": group_id}) or {}).get("cover_lock")
        if not isinstance(cover_reference, dict):
            raise SeriesError("cover_lock_failed", "无法锁定本组封面版本，请重试", 409)
    now = utcnow()
    for page in pages:
        if page.get("role") != "detail":
            continue
        # Claim with the exact task id that will be inserted: a concurrent caller can
        # never create a second task for the same page.
        task_id = f"wtask_{uuid.uuid4().hex}"
        claimed = db.whole_series_pages.find_one_and_update(
            {"_id": page["_id"], "whole_task_id": None},
            {"$set": {"whole_task_id": task_id, "status": "queued",
                      "cover_reference": cover_reference, "updated_at": now}},
        )
        if claimed is None:
            continue
        try:
            page_prompt = _page_prompt(
                group.get("prompt") or "",
                "detail",
                int(page.get("order") or 0),
                int(group.get("page_count") or 2),
            )
            task_doc = _new_page_task_doc(
                db, group, claimed, role="detail",
                prompt=page_prompt, cover_reference=cover_reference,
                task_id=task_id,
            )
            db.whole_tasks.insert_one(task_doc)
            launch(task_id)
        except Exception as exc:
            # Release the claim so a later retry can create the page task.
            db.whole_series_pages.update_one(
                {"_id": page["_id"], "whole_task_id": task_id},
                {"$set": {"whole_task_id": None, "status": "failed",
                          "error": _error_text(exc), "updated_at": now}},
            )
            raise
    db.whole_series_groups.update_one(
        {"_id": group_id},
        {"$set": {"interiors_started_at": now, "updated_at": now}},
    )
    group = _group(db, group_id)
    return _group_out(
        db, group,
        list(db.whole_series_pages.find({"group_id": group_id}).sort("order", 1)),
        merchant_id=merchant_id,
    )


def retry_page(
    db: Any,
    settings: Settings | Any,
    group_id: str,
    page_id: str,
    *,
    launch: Callable[[str], None],
    merchant_id: str = "",
) -> dict[str, Any]:
    """Retry one failed page, or resume its saved task without creating a new page."""
    require_enabled(settings)
    group = _group(db, group_id)
    page = _page(db, group_id, page_id)
    task_id = str(page.get("whole_task_id") or "")
    if task_id:
        task = db.whole_tasks.find_one({"_id": task_id})
        if task is None:
            task_id = ""
        elif task.get("status") in {"queued", "running"}:
            return page_out(db, page, merchant_id=merchant_id)
        elif task.get("status") == "succeeded":
            return page_out(db, page, merchant_id=merchant_id)
        else:
            now = utcnow()
            db.whole_tasks.update_one(
                {"_id": task_id},
                {"$set": {
                    "status": "queued", "stage": "pending", "progress": 0,
                    "generation": {"status": "queued", "error": None},
                    "provider.status": "queued", "error": None,
                    "series.status": "queued", "updated_at": now,
                }},
            )
            _update_page(db, page_id, {"status": "queued", "error": None})
            _refresh_group(db, group_id)
            launch(task_id)
            return page_out(db, _page(db, group_id, page_id), merchant_id=merchant_id)
    role = str(page.get("role") or "cover")
    cover_reference = page.get("cover_reference")
    if role == "detail" and not cover_reference:
        raise SeriesError("cover_not_ready", "请先生成内页并锁定封面版本", 409)
    prompt = _page_prompt(
        group.get("prompt") or "", role, int(page.get("order") or 0),
        int(group.get("page_count") or 2),
    )
    task_doc = _new_page_task_doc(
        db, group, page, role=role, prompt=prompt, cover_reference=cover_reference
    )
    db.whole_tasks.insert_one(task_doc)
    new_task_id = str(task_doc["_id"])
    now = utcnow()
    db.whole_series_pages.update_one(
        {"_id": page_id},
        {"$set": {"whole_task_id": new_task_id, "status": "queued",
                  "error": None, "updated_at": now}},
    )
    _refresh_group(db, group_id)
    launch(new_task_id)
    return page_out(db, _page(db, group_id, page_id), merchant_id=merchant_id)


def on_cover_complete(
    db: Any,
    settings: Settings | Any,
    store: Any,
    client: Any,
    group_id: str,
    version: dict[str, Any],
) -> None:
    """Auto-start interiors once per group; duplicate callback deliveries are harmless."""
    group = db.whole_series_groups.find_one({"_id": group_id})
    if group is None or not group.get("auto_generate_interiors"):
        return
    if group.get("interiors_started_at"):
        return

    def _launch(task_id: str) -> None:
        # Staying inline keeps the auto chain ordered cover -> interiors and avoids
        # needing a BackgroundTasks object inside a background worker.
        run_page_generation(db, settings, store, client, task_id)

    try:
        start_interiors(
            db, settings, group_id,
            cover_version_id=str(version["_id"]),
            cover_output_id=None,
            launch=_launch,
        )
    except Exception:  # noqa: BLE001 - auto-start must not turn a good cover into a failure
        logger.exception("auto interior start failed for group %s", group_id)


def resume_tasks(db: Any, settings: Settings | Any, store: Any, client: Any) -> None:
    """Resume series pages after a restart without triggering a new paid Agent run."""
    tasks = list(db.whole_tasks.find({
        "series.run_id": {"$exists": True},
        "series.status": {"$in": ["queued", "running"]},
        "status": {"$in": ["queued", "running", "failed"]},
    }))
    for task in tasks:
        task_id = str(task["_id"])
        db.whole_tasks.update_one(
            {"_id": task_id},
            {"$set": {
                "status": "queued", "stage": "pending", "progress": 0,
                "generation": {"status": "queued", "error": None},
                "provider.status": "queued", "error": None,
                "series.status": "queued", "updated_at": utcnow(),
            }},
        )
        _update_page(db, str((task.get("series") or {}).get("page_id") or ""),
                     {"status": "queued", "error": None})
        try:
            run_page_generation(db, settings, store, client, task_id)
        except Exception:  # noqa: BLE001 - one page must not stop the others
            logger.exception("series resume failed for %s", task_id)


# --------------------------------------------------------------------------------------
# HTML editor save back into the page
# --------------------------------------------------------------------------------------

def _decode_html_png(payload: Any, canvas: dict[str, Any]) -> tuple[bytes, int, int]:
    raw = str(getattr(payload, "png_base64", "") or "").strip()
    if raw.startswith("data:image/png;base64,"):
        raw = raw.split(",", 1)[1]
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SeriesError("invalid_png", "保存内容不是有效的 PNG 图片", 422) from exc
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SeriesError("invalid_png", "保存内容不是有效的 PNG 图片", 422)
    width, height = validate_png(data, canvas, status_code=422)
    listed = (int(getattr(payload, "width", 0) or 0), int(getattr(payload, "height", 0) or 0))
    if listed != (width, height):
        raise SeriesError("invalid_canvas_size", "编辑器声明的画布尺寸与图片不一致", 422)
    return data, width, height


def _require_artifact_owner(
    db: Any, task_id: str, payload: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bound an editor result to the replacement that produced its HTML.

    The artifact must be the HTML of a replacement task of this exact page, that
    replacement must have been opened from the same source version/output, and its
    replacement list is the authoritative record of which photos the editor changed.
    """
    artifact_id = str(getattr(payload, "artifact_id", "") or "").strip()
    if not artifact_id:
        raise SeriesError("artifact_required", "缺少编辑器文件，请重新打开编辑器", 422)
    artifact = db.whole_artifacts.find_one({"_id": artifact_id})
    if artifact is None:
        raise SeriesError("artifact_not_found", "编辑器文件不属于当前页", 404)
    if str(artifact.get("kind") or "") != "html":
        raise SeriesError("artifact_not_html", "该文件不是可编辑的 HTML 结果", 422)
    replacement = db.background_replace_tasks.find_one(
        {"_id": artifact.get("task_id"), "whole_task_id": task_id}
    )
    if replacement is None:
        raise SeriesError("artifact_not_owned", "编辑器文件不属于当前页", 409)
    if str(replacement.get("source_version_id") or "") != str(payload.source_version_id):
        raise SeriesError("version_conflict", "编辑器来源版本已变化，请重新打开编辑器", 409)
    expected = str(getattr(payload, "expected_output_id", "") or "")
    if not expected:
        raise SeriesError("output_required", "缺少编辑器来源结果，请重新打开编辑器", 422)
    if str(replacement.get("source_output_id") or "") != expected:
        raise SeriesError("version_conflict", "编辑器来源结果已变化，请重新打开编辑器", 409)
    return artifact, replacement


def _html_save_refs(
    db: Any,
    task: dict[str, Any],
    version: dict[str, Any],
    payload: Any,
    replacement: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build the saved version's photo associations from the validated replacement.

    The browser sends the page's current defaults, which are the *old* photos. They are
    therefore not trusted as the new list: the source version's refs are preserved, the
    replacement record's known target -> new-asset mapping is applied with the same target
    resolution as an ordinary original replacement, and payload entries may only confirm
    targets for assets the replacement record actually introduced.
    """
    series = task.get("series") or {}
    logo_ids = {str((task.get("input") or {}).get("logo_asset_id") or "")}
    logo_ids.update(
        str(value) for value in (task.get("brand") or {}).get("logo_asset_ids") or []
    )
    logo_ids.discard("")
    style_ids = {
        str(value) for value in
        (version.get("series_style_reference_ids") or series.get("style_reference_ids") or [])
    }
    cover_reference = version.get("series_cover_reference") or series.get("cover_reference") or {}
    if cover_reference.get("asset_id"):
        logo_ids.add(str(cover_reference["asset_id"]))

    base = version.get("series_content_refs")
    if base is None and version.get("parent_version_id"):
        parent = db.whole_versions.find_one(
            {"_id": version["parent_version_id"], "task_id": version["task_id"]}
        )
        base = (parent or {}).get("series_content_refs")
    if base is None:
        base = series.get("content_refs") or []
    refs: list[dict[str, Any]] = []
    for item in base:
        asset_id = str(item.get("asset_id") if isinstance(item, dict) else item or "").strip()
        if not asset_id or asset_id in logo_ids or asset_id in style_ids:
            continue
        target = str(item.get("target") or "") if isinstance(item, dict) else ""
        refs.append({"asset_id": asset_id, "target": target})

    replacements = (
        [item for item in (replacement.get("input") or {}).get("replacements") or []
         if isinstance(item, dict)]
        or [item for item in replacement.get("replacements") or [] if isinstance(item, dict)]
    )
    introduced: set[str] = set()
    for item in replacements:
        asset_id = str(item.get("asset_id") or "").strip()
        target = str(item.get("target") or "")
        if not asset_id or asset_id in logo_ids or asset_id in style_ids:
            continue
        if db.assets.find_one({"_id": asset_id}) is None:
            raise SeriesError("asset_missing", "替换记录引用了不存在的素材", 422)
        introduced.add(asset_id)
        match = next(
            (
                ref for ref in refs
                if target and ref.get("target") and (
                    ref["target"] == target
                    or target in ref["target"]
                    or ref["target"] in target
                )
            ),
            None,
        )
        if match is not None:
            match["asset_id"] = asset_id
            if target:
                match["target"] = target
        elif not any(ref["asset_id"] == asset_id for ref in refs):
            refs.append({"asset_id": asset_id, "target": target})

    # HTML-side adjustments only confirm targets for photos this replacement introduced;
    # the browser's stale default list cannot resurrect a replaced original.
    for item in getattr(payload, "content_refs", None) or []:
        if isinstance(item, dict):
            asset_id = str(item.get("asset_id") or "").strip()
            target = str(item.get("target") or "")
        else:
            asset_id = str(getattr(item, "asset_id", "") or "").strip()
            target = str(getattr(item, "target", "") or "")
        if asset_id not in introduced or not target:
            continue
        for ref in refs:
            if ref["asset_id"] == asset_id:
                ref["target"] = target
    return refs


def _require_photo_only_replacement(task: dict[str, Any], replacement: dict[str, Any]) -> None:
    """A series HTML save must come from a record that only replaced photos.

    The series layer always composites the group's official Logo. A record that also asked
    the Agent to replace the Logo would bake a second Logo into the saved canvas.
    """
    from app.services import original_replacement

    requested = replacement.get("replacements") or (replacement.get("input") or {}).get("replacements")
    if original_replacement.replacement_requests_logo(task, requested):
        raise SeriesError(
            "series_logo_replacement_not_allowed",
            "这条原照替换记录请求了 Logo 替换，保存会叠加出第二个 Logo。请重新生成仅替换照片的原照替换后再保存到本页。",
            422,
        )


def html_save(
    db: Any,
    settings: Settings | Any,
    store: Any,
    group_id: str,
    page_id: str,
    payload: Any,
    *,
    merchant_id: str = "",
) -> dict[str, Any]:
    """Save the isolated HTML editor's canvas as a new version of this exact page.

    Contract: only the current version's own editor result is accepted, its PNG must
    decode at the group canvas size, and the commit is idempotent per editor session plus
    payload hash so a retried request cannot create a second version.
    """
    require_enabled(settings)
    group = _group(db, group_id)
    page = _page(db, group_id, page_id)
    task_id = str(page.get("whole_task_id") or "")
    task = db.whole_tasks.find_one({"_id": task_id}) if task_id else None
    if task is None:
        raise SeriesError("page_not_ready", "该页尚未生成", 409)
    if task.get("status") != "succeeded":
        raise SeriesError("page_not_ready", "该页尚未生成可保存的成图", 409)
    source = db.whole_versions.find_one(
        {"_id": str(payload.source_version_id), "task_id": task_id}
    )
    if source is None:
        raise SeriesError("invalid_source", "来源版本不属于当前页", 422)
    session_id = str(getattr(payload, "session_id", "") or "").strip()
    if len(session_id) < 8:
        raise SeriesError("invalid_session", "编辑器会话无效，请重新打开编辑器", 422)
    canvas = dict(group.get("canvas") or task.get("canvas") or {})
    if not canvas.get("width") or not canvas.get("height"):
        raise SeriesError("canvas_missing", "系列组缺少画布尺寸", 409)
    data, width, height = _decode_html_png(payload, canvas)
    _artifact, replacement = _require_artifact_owner(db, task_id, payload)
    _require_photo_only_replacement(task, replacement)
    refs = _html_save_refs(db, task, source, payload, replacement)
    request_key = hashlib.sha256(
        json.dumps(
            {
                "session": session_id,
                "source": str(payload.source_version_id),
                "expected_output": getattr(payload, "expected_output_id", None),
                "artifact": getattr(payload, "artifact_id", None),
                "sha256": hashlib.sha256(data).hexdigest(),
                "refs": refs,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    claim = {
        "_id": "whsave_" + request_key[:32],
        "group_id": group_id,
        "page_id": page_id,
        "task_id": task_id,
        "session_id": session_id,
        "source_version_id": str(payload.source_version_id),
        "png_sha256": hashlib.sha256(data).hexdigest(),
        "status": "pending",
        "version_id": None,
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }
    try:
        db.whole_series_html_saves.insert_one(claim)
    except DuplicateKeyError:
        existing = db.whole_series_html_saves.find_one({"_id": claim["_id"]})
        if existing and existing.get("version_id"):
            return page_out(db, _page(db, group_id, page_id), merchant_id=merchant_id)
        raise SeriesError("save_in_progress", "这一页正在保存，请稍候刷新", 409) from None
    token = uuid.uuid4().hex
    saved_asset_ids: list[str] = []
    committed = False
    try:
        # Every publisher must hold the page lock. The filter is the compare-and-set:
        # a concurrent edit or a second editor session that already published makes the
        # claim fail instead of overwriting newer work.
        prior = db.whole_tasks.find_one_and_update(
            {
                "_id": task_id,
                "status": "succeeded",
                "edit.status": {"$ne": "running"},
                "current_version_id": str(payload.source_version_id),
                "current_output_id": str(payload.expected_output_id),
                "series.html_save_token": {"$exists": False},
            },
            {"$set": {"edit.status": "running", "series.html_save_token": token}},
            return_document=ReturnDocument.BEFORE,
        )
        if prior is None:
            raise SeriesError("version_conflict", "这一页已有更新的版本或正在处理，请重新打开编辑器", 409)
        version_number = _next_series_version(db, task_id)
        saved_base = _save_output_asset(
            db, settings, store, task_id=task_id, version=version_number, data=data,
            role="series_html_base", target_size=(width, height),
        )
        saved_asset_ids.append(str(saved_base["asset_id"]))
        base_output = {
            "id": "series_html_base",
            "channel": "html",
            "role": "series_html_base",
            "label": "无 Logo 底图",
            "asset_id": str(saved_base["asset_id"]),
            "width": width,
            "height": height,
            "selectable": False,
            "model_input": None,
            "created_at": utcnow(),
        }
        output_asset_id, outputs, logo_fields = finalize_series_outputs(
            db, settings, store, task=task, version=version_number,
            outputs=[base_output], canvas=canvas,
        )
        saved_asset_ids.extend(
            str(item["asset_id"]) for item in outputs
            if item["asset_id"] and str(item["asset_id"]) not in saved_asset_ids
        )
        document = _version_document(
            task_id=task_id,
            version=version_number,
            parent_version_id=str(source["_id"]),
            output_asset_id=output_asset_id,
            instruction="HTML 编辑器保存到本页",
            operation="series_html_save",
            width=width,
            height=height,
            model_input={
                "channel": "editor",
                "endpoint": "poster-series-html-save",
                "model": "",
                "prompt": "",
                "parameters": {
                    "session_id": session_id,
                    "artifact_id": str(payload.artifact_id),
                    "png_sha256": claim["png_sha256"],
                    "replacement_id": str(replacement["_id"]),
                },
                "images": [],
            },
            outputs=outputs,
        )
        document["series_content_refs"] = refs
        document["series_cover_reference"] = (
            source.get("series_cover_reference") or (task.get("series") or {}).get("cover_reference")
        )
        document["series_role"] = (task.get("series") or {}).get("role")
        document["series_page_id"] = page_id
        document.update(logo_fields)
        document["series_html_save"] = {
            "session_id": session_id,
            "artifact_id": str(payload.artifact_id),
            "replacement_id": str(replacement["_id"]),
            "source_version_id": str(source["_id"]),
            "png_sha256": claim["png_sha256"],
        }
        _commit_version(
            db, store, task_id=task_id, version_doc=document,
            generation=False, edit_outcome="success",
            message=f"已保存编辑器结果 v{version_number}。",
            expect={"series.html_save_token": token},
            unset=["series.html_save_token"],
        )
        committed = True
        final_output = next(item for item in outputs if item.get("selectable"))
        _update_page(db, page_id, {
            "status": "succeeded",
            "version_id": str(document["_id"]),
            "output_id": final_output.get("id"),
            "content_refs": refs,
            "error": None,
        })
        _refresh_group(db, group_id)
        db.whole_series_html_saves.update_one(
            {"_id": claim["_id"]},
            {"$set": {
                "status": "saved",
                "version_id": str(document["_id"]),
                "output_id": final_output.get("id"),
                "updated_at": utcnow(),
            }},
        )
    except Exception:
        if not committed:
            db.whole_tasks.update_one(
                {"_id": task_id, "series.html_save_token": token},
                {"$set": {"edit.status": "idle"},
                 "$unset": {"series.html_save_token": ""}},
            )
            for asset_id in saved_asset_ids:
                try:
                    _delete_asset(db, store, asset_id)
                except Exception:  # noqa: BLE001 - cleanup must not mask the real error
                    logger.exception("html save cleanup failed for %s", asset_id)
        db.whole_series_html_saves.delete_one({"_id": claim["_id"], "version_id": None})
        raise
    return page_out(db, _page(db, group_id, page_id), merchant_id=merchant_id)


def _next_series_version(db: Any, task_id: str) -> int:
    from app.services.whole_image import _next_version

    return _next_version(db, task_id)


# --------------------------------------------------------------------------------------
# Downloads
# --------------------------------------------------------------------------------------

_UNSAFE_FILE_CHARS = re.compile(r'[<>:"/\|?*\x00-\x1f]')


def _safe_name(value: str) -> str:
    cleaned = _UNSAFE_FILE_CHARS.sub("_", str(value or "")).strip().strip(".")
    return cleaned[:120] or "poster"


def _current_page_asset(db: Any, page: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    task_id = str(page.get("whole_task_id") or "")
    task = db.whole_tasks.find_one({"_id": task_id}) if task_id else None
    if task is None:
        raise SeriesError("page_not_ready", "该页尚未生成", 409)
    version = _page_version(db, page)
    output = _page_output(version, task.get("current_output_id"))
    asset_id = (output or {}).get("asset_id")
    if not asset_id:
        raise SeriesError("page_not_ready", "该页尚未生成可下载的成品", 409)
    asset = db.assets.find_one({"_id": asset_id})
    if asset is None:
        raise SeriesError("asset_missing", "该页成品已不存在", 404)
    return task, version or {}, asset


def _asset_bytes(store: Any, asset: dict[str, Any]) -> bytes:
    key = str(asset.get("prepared_path") or asset.get("path") or "")
    if not key:
        raise SeriesError("asset_missing", "成图文件不存在", 404)
    try:
        return store.get_bytes(key)
    except Exception as exc:  # noqa: BLE001
        raise SeriesError("asset_missing", "成图文件不存在", 404) from exc


def page_download(db: Any, store: Any, group_id: str, page_id: str) -> tuple[bytes, str]:
    page = _page(db, group_id, page_id)
    _, _, asset = _current_page_asset(db, page)
    order = int(page.get("order") or 0) + 1
    label = {"cover": "封面", "detail": "介绍页"}.get(page.get("role"), "图片")
    filename = f"{order:02d}-{label}.png"
    return _asset_bytes(store, asset), _safe_name(filename)


def group_download(db: Any, store: Any, group_id: str) -> tuple[bytes, str]:
    group = _group(db, group_id)
    pages = list(db.whole_series_pages.find({"group_id": group_id}).sort("order", 1))
    if not pages:
        raise SeriesError("group_empty", "系列组没有页面", 409)
    missing = [
        int(page.get("order") or 0) + 1
        for page in pages if str(page.get("status")) != "succeeded"
    ]
    if missing:
        raise SeriesError("group_incomplete", "该组仍有页面未完成", 409)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for page in pages:
            _, _, asset = _current_page_asset(db, page)
            order = int(page.get("order") or 0) + 1
            label = "封面" if page.get("role") == "cover" else "介绍页"
            name = f"{order:02d}-{label}.png"
            archive.writestr(_safe_name(name), _asset_bytes(store, asset))
    filename = f"poster-series-{_safe_name(str(group.get('order', 0) + 1))}.zip"
    return buffer.getvalue(), filename
