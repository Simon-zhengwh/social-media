"""Offline, conservative photo grouping without semantic/model inference.

Features describe the displayed first frame. Groups use complete-link matching:
every pair must pass the thresholds (or have the same content SHA), so nearby
intermediate photos cannot bridge two dissimilar subjects.
"""

from __future__ import annotations

import io
import math
import statistics
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from app.services import material_local_match as local_match

VERSION = "visual-hash-v2"
GROUPING_VERSION = "complete-link-geometry-v5"
MAX_IMAGE_PIXELS = 64_000_000
MAX_IMAGE_BYTES = 64 * 1024 * 1024
_THUMBNAIL_SIZE = 256
# Global layout often describes the plate/table rather than the food. Require
# the central 60% of each axis to agree too. Favor near frames over grouping
# changed angles of the same dish: all three subject gates must pass.
_MAX_SUBJECT_PHASH = 12
_MAX_SUBJECT_DHASH = 14
_MAX_SUBJECT_COLOR = 0.20
_DCT_SIZE = 32
_COSINES = tuple(
    tuple(math.cos(math.pi * (2 * position + 1) * frequency / (2 * _DCT_SIZE))
          for position in range(_DCT_SIZE))
    for frequency in range(8)
)


def _hash_bits(values: list[bool]) -> str:
    value = 0
    for bit in values:
        value = (value << 1) | bit
    return f"{value:016x}"


def _phash(image: Image.Image) -> str:
    gray = image.convert("L").resize((_DCT_SIZE, _DCT_SIZE), Image.Resampling.LANCZOS)
    pixels = list(gray.get_flattened_data())
    rows = [
        [sum(pixels[y * _DCT_SIZE + x] * cosine[x] for x in range(_DCT_SIZE))
         for cosine in _COSINES]
        for y in range(_DCT_SIZE)
    ]
    coefficients = [
        sum(rows[y][u] * _COSINES[v][y] for y in range(_DCT_SIZE))
        for v in range(8) for u in range(8)
    ]
    # A uniform field has no AC signal; ignore tiny floating point DCT residue.
    coefficients = [0.0 if abs(value) < 1e-7 else value for value in coefficients]
    median = statistics.median(coefficients[1:])
    return _hash_bits([False] + [value > median for value in coefficients[1:]])


def _dhash(image: Image.Image) -> str:
    pixels = list(image.convert("L").resize((9, 8), Image.Resampling.LANCZOS).get_flattened_data())
    return _hash_bits([pixels[y * 9 + x] > pixels[y * 9 + x + 1]
                       for y in range(8) for x in range(8)])


def _color(image: Image.Image) -> list[float]:
    """Soft HS histogram plus luminance, stable under small exposure changes.

    Saturated subject pixels get more weight than neutral plate/background
    pixels. Gray has one shared bin, so its arbitrary hue cannot skew distance.
    """
    sample = image.resize((48, 48), Image.Resampling.BOX).convert("HSV")
    hs = [0.0] * 17
    luminance = [0.0] * 4
    for hue, saturation, value in sample.get_flattened_data():
        saturation_level = saturation / 255 * 2
        hue_position = hue / 256 * 8
        hue_low = int(hue_position)
        hue_fraction = hue_position - hue_low
        weight = 0.5 + saturation / 255
        if saturation_level < 1:
            hs[0] += (1 - saturation_level) * weight
        for level in (1, 2):
            saturation_weight = max(0.0, 1 - abs(saturation_level - level)) * weight
            hs[1 + (level - 1) * 8 + hue_low] += saturation_weight * (1 - hue_fraction)
            hs[1 + (level - 1) * 8 + (hue_low + 1) % 8] += saturation_weight * hue_fraction
        value_position = value / 255 * 3
        low = min(3, int(value_position))
        fraction = value_position - low
        luminance[low] += 1 - fraction
        luminance[min(3, low + 1)] += fraction
    total_hs = sum(hs)
    total_luminance = sum(luminance)
    return [0.85 * value / total_hs for value in hs] + [
        0.15 * value / total_luminance for value in luminance
    ]


def _quality(image: Image.Image, width: int, height: int) -> float:
    gray = image.convert("L")
    pixels = list(gray.get_flattened_data())
    sample_width, sample_height = gray.size
    differences = [
        abs(pixels[y * sample_width + x] - pixels[y * sample_width + x + 1])
        + abs(pixels[y * sample_width + x] - pixels[(y + 1) * sample_width + x])
        for y in range(sample_height - 1) for x in range(sample_width - 1)
    ]
    sharpness = 1 - math.exp(-sum(differences) / max(1, len(differences)) / 8)
    exposure = 1 - sum(value <= 8 or value >= 247 for value in pixels) / len(pixels)
    resolution = min(1.0, math.log2(max(1, width * height)) / 24)
    return round(0.55 * sharpness + 0.30 * exposure + 0.15 * resolution, 6)


def extract_features(data: bytes) -> dict:
    """Extract bounded local features, raising ValueError for unusable input."""
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("image is empty or too large")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                width, height = source.size
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise ValueError("image is too large")
                orientation = source.getexif().get(274, 1)
                if orientation in (5, 6, 7, 8):
                    width, height = height, width
                # JPEG draft reduces decode memory. All formats are pixel-limited
                # before loading, then reduced before conversion/transposition.
                source.draft("RGB", (_THUMBNAIL_SIZE, _THUMBNAIL_SIZE))
                source.thumbnail((_THUMBNAIL_SIZE, _THUMBNAIL_SIZE), Image.Resampling.LANCZOS)
                thumbnail = ImageOps.exif_transpose(source)
                if thumbnail.mode in ("RGBA", "LA") or "transparency" in thumbnail.info:
                    rgba = thumbnail.convert("RGBA")
                    rgb = Image.new("RGB", rgba.size, "white")
                    rgb.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    rgb = thumbnail.convert("RGB")
                sample_width, sample_height = rgb.size
                subject = rgb.crop((round(sample_width * 0.2), round(sample_height * 0.2),
                                    round(sample_width * 0.8), round(sample_height * 0.8)))
                return {
                    "version": VERSION,
                    "phash": _phash(rgb),
                    "dhash": _dhash(rgb),
                    "color": _color(rgb),
                    "subject_phash": _phash(subject),
                    "subject_dhash": _dhash(subject),
                    "subject_color": _color(subject),
                    "quality": _quality(rgb, width, height),
                    "width": width,
                    "height": height,
                }
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise ValueError("image could not be decoded safely") from exc


def hash_distance(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def color_distance(left: list[float], right: list[float]) -> float:
    """Hellinger distance of normalized color distributions, in [0, 1]."""
    if len(left) != len(right) or not left:
        return 1.0
    return min(1.0, math.sqrt(sum((math.sqrt(max(0.0, a)) - math.sqrt(max(0.0, b))) ** 2
                                 for a, b in zip(left, right)) / 2))


def _sort_key(item: dict) -> tuple[str, str, str]:
    path = str(item.get("relative_path", ""))
    return path.casefold(), path, str(item["id"])


def _same_content(left: dict, right: dict) -> bool:
    return bool(left.get("sha256")) and left["sha256"] == right.get("sha256")


def _near(left: dict, right: dict, max_phash: int, max_dhash: int, max_color: float) -> bool:
    if _same_content(left, right):
        return True
    a, b = left["features"], right["features"]
    if not all(feature.get(key) for feature in (a, b)
               for key in ("subject_phash", "subject_dhash", "subject_color")):
        return False
    if a.get("version") != VERSION or b.get("version") != VERSION:
        return False
    phash = hash_distance(a["phash"], b["phash"])
    dhash = hash_distance(a["dhash"], b["dhash"])
    if (
        phash <= max_phash
        and dhash <= max_dhash
        and color_distance(a["color"], b["color"]) <= max_color
        and hash_distance(a["subject_phash"], b["subject_phash"]) <= _MAX_SUBJECT_PHASH
        and hash_distance(a["subject_dhash"], b["subject_dhash"]) <= _MAX_SUBJECT_DHASH
        and color_distance(a["subject_color"], b["subject_color"]) <= _MAX_SUBJECT_COLOR
    ):
        return True
    # Changed framing can randomize positional hashes. A second local check
    # must prove the central content aligns geometrically and photometrically.
    return bool(
        a.get("alignment") and b.get("alignment")
        and phash <= 24 and dhash <= 28
        and color_distance(a["color"], b["color"]) <= 0.20
        and color_distance(a["subject_color"], b["subject_color"]) <= 0.20
        and local_match.matches(a["alignment"], b["alignment"])
    )


def cluster_files(items: list[dict], *, max_phash: int = 10, max_dhash: int = 14,
                  max_color: float = 0.22) -> list[list[dict]]:
    """Return deterministic strict groups; preserve each input file identity."""
    groups: list[list[dict]] = []
    for item in sorted(items, key=_sort_key):
        for group in groups:
            if all(_near(item, member, max_phash, max_dhash, max_color) for member in group):
                group.append(item)
                break
        else:
            groups.append([item])
    return groups


def _distance(left: dict, right: dict) -> float:
    if _same_content(left, right):
        return 0.0
    a, b = left["features"], right["features"]
    return (hash_distance(a["phash"], b["phash"]) / 64
            + hash_distance(a["dhash"], b["dhash"]) / 64
            + color_distance(a["color"], b["color"])) / 3


def _distinct(left: dict, right: dict) -> bool:
    if _same_content(left, right):
        return False
    a, b = left["features"], right["features"]
    phash = hash_distance(a["phash"], b["phash"])
    dhash = hash_distance(a["dhash"], b["dhash"])
    if phash == dhash == 0:
        return False
    return not (phash <= 2 and dhash <= 2 and color_distance(a["color"], b["color"]) <= 0.06)


def choose_representatives(members: list[dict], count: int = 1,
                           cached_ids: set[str] | None = None) -> list[str]:
    """Choose at most two existing members, preferring valid reusable results."""
    if not members or count <= 0:
        return []
    ordered = sorted(members, key=_sort_key)
    cached = cached_ids or set()
    candidates = [item for item in ordered if item["id"] in cached] or ordered
    centrality = {
        item["id"]: sum(_distance(item, other) for other in ordered) / len(ordered)
        for item in candidates
    }
    best_centrality = min(centrality.values())
    central = [item for item in candidates if centrality[item["id"]] <= best_centrality + 0.03]
    first = min(central, key=lambda item: (-item["features"].get("quality", 0),
                                           centrality[item["id"]], _sort_key(item)))
    result = [first["id"]]
    if count < 2:
        return result
    diverse = [item for item in ordered if item["id"] != first["id"] and _distinct(first, item)]
    if not diverse:
        return result
    diverse = [item for item in diverse if item["id"] in cached] or diverse
    second = min(diverse, key=lambda item: (-_distance(first, item),
                                            -item["features"].get("quality", 0), _sort_key(item)))
    return result + [second["id"]]
