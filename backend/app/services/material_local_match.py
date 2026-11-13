"""Bounded local geometry checks for photos missed by layout hashes.

SIFT correspondences must agree with one nondegenerate transformation and with
the aligned central pixels. A shared table or plate alone is insufficient.
These records contain no file paths; comparison performs no I/O or model calls.
"""
from __future__ import annotations

import base64
import binascii
from functools import lru_cache
import io
import warnings
import zlib

import cv2 as cv
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

VERSION = "sift-geometry-v1"
MAX_POINTS = 700
_MAX_SIDE = 512
_GRAY_SIDE = 128
_MAX_IMAGE_BYTES = 64 * 1024 * 1024
_MAX_IMAGE_PIXELS = 64_000_000
_DESCRIPTOR_WIDTH = 128


def _subject_mask(height: int, width: int, *, core: bool = False) -> np.ndarray:
    mask = np.zeros((height, width), np.uint8)
    radii = (.22, .24) if core else (.36, .35)
    cv.ellipse(mask, (width // 2, int(height * .53)),
               (int(width * radii[0]), int(height * radii[1])), 0, 0, 360, 255, -1)
    return mask


def extract_features(jpeg: bytes) -> dict:
    """Extract JSON-safe records from a bounded, EXIF-corrected local preview."""
    if not jpeg or len(jpeg) > _MAX_IMAGE_BYTES:
        raise ValueError("image is empty or too large")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(jpeg)) as source:
                if source.width * source.height > _MAX_IMAGE_PIXELS:
                    raise ValueError("image is too large")
                source.draft("RGB", (_MAX_SIDE, _MAX_SIDE))
                source.thumbnail((_MAX_SIDE, _MAX_SIDE), Image.Resampling.LANCZOS)
                image = ImageOps.exif_transpose(source).convert("RGB")
                rgb = np.asarray(image)
        gray = cv.cvtColor(rgb, cv.COLOR_RGB2GRAY)
        height, width = gray.shape
        keypoints, descriptors = cv.SIFT_create(nfeatures=MAX_POINTS, contrastThreshold=.025).detectAndCompute(
            gray, _subject_mask(height, width))
        # OpenCV can exceed nfeatures on tied responses. Bound persisted records.
        keypoints = keypoints[:MAX_POINTS]
        descriptor_bytes = descriptors[:len(keypoints)].astype(np.uint8).tobytes() if descriptors is not None else b""
        small = cv.resize(gray, (_GRAY_SIDE, _GRAY_SIDE), interpolation=cv.INTER_AREA)
        return {"version": VERSION,
                "points": [[round(point.pt[0] / width, 7), round(point.pt[1] / height, 7)] for point in keypoints],
                "descriptors": base64.b64encode(descriptor_bytes).decode("ascii"),
                "gray": base64.b64encode(zlib.compress(small.tobytes(), level=3)).decode("ascii")}
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning, cv.error) as exc:
        raise ValueError("image could not be decoded safely") from exc


@lru_cache(maxsize=384)
def _decode_blobs(descriptors: str, gray: str, count: int) -> tuple[np.ndarray, np.ndarray]:
    raw_descriptors = base64.b64decode(descriptors, validate=True)
    if len(raw_descriptors) != count * _DESCRIPTOR_WIDTH:
        raise ValueError("descriptor count does not match keypoints")
    compressed = base64.b64decode(gray, validate=True)
    decoder = zlib.decompressobj()
    raw_gray = decoder.decompress(compressed, _GRAY_SIDE * _GRAY_SIDE + 1)
    if (len(raw_gray) != _GRAY_SIDE * _GRAY_SIDE or not decoder.eof
            or decoder.unconsumed_tail or decoder.unused_data):
        raise ValueError("invalid bounded grayscale buffer")
    desc = np.frombuffer(raw_descriptors, np.uint8).reshape(count, _DESCRIPTOR_WIDTH)
    image = np.frombuffer(raw_gray, np.uint8).reshape(_GRAY_SIDE, _GRAY_SIDE)
    return desc, image


def _decode(record: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if not isinstance(record, dict) or record.get("version") != VERSION:
        raise ValueError("unsupported local feature version")
    points = record.get("points")
    descriptors, gray = record.get("descriptors"), record.get("gray")
    if (not isinstance(points, list) or len(points) > MAX_POINTS
            or not isinstance(descriptors, str) or len(descriptors) > 120_000
            or not isinstance(gray, str) or len(gray) > 24_000):
        raise ValueError("local feature record exceeds bounds")
    if any(not isinstance(point, (list, tuple)) or len(point) != 2
           or any(not isinstance(value, (int, float)) for value in point) for point in points):
        raise ValueError("keypoints must contain two numeric coordinates")
    array = np.asarray(points, np.float32).reshape((-1, 2))
    if array.shape != (len(points), 2) or not np.isfinite(array).all() or (array < 0).any() or (array > 1).any():
        raise ValueError("invalid normalized keypoints")
    desc, image = _decode_blobs(descriptors, gray, len(points))
    return array * _GRAY_SIDE, desc, image


def valid_features(record: dict) -> bool:
    try:
        _decode(record)
        return True
    except (ValueError, TypeError, OverflowError, binascii.Error, zlib.error):
        return False


def _mutual_matches(left: np.ndarray, right: np.ndarray):
    matcher = cv.BFMatcher(cv.NORM_L2)
    forward = matcher.knnMatch(left, right, k=2)
    accepted_forward = [pair[0] for pair in forward if len(pair) == 2
                        and pair[0].distance < .75 * pair[1].distance]
    if len(accepted_forward) < 20:
        return []
    reverse = matcher.knnMatch(right, left, k=2)
    accepted_reverse = {(pair[0].trainIdx, pair[0].queryIdx) for pair in reverse
                        if len(pair) == 2 and pair[0].distance < .75 * pair[1].distance}
    return [match for match in accepted_forward
            if (match.queryIdx, match.trainIdx) in accepted_reverse]


def _valid_homography(matrix: np.ndarray) -> bool:
    if not np.isfinite(matrix).all() or abs(matrix[2, 2]) < 1e-9:
        return False
    matrix = matrix / matrix[2, 2]
    if np.linalg.cond(matrix) > 1e6:
        return False
    corners = np.float32([[0, 0], [127, 0], [127, 127], [0, 127]]).reshape(-1, 1, 2)
    mapped = cv.perspectiveTransform(corners, matrix)
    return (bool(np.isfinite(mapped).all()) and bool((np.abs(mapped) <= _GRAY_SIDE * 4).all())
            and cv.isContourConvex(mapped) and .15 <= cv.contourArea(mapped) / (_GRAY_SIDE ** 2) <= 6)


def _photometric(left: np.ndarray, right: np.ndarray, matrix: np.ndarray, *, core: bool = False) -> tuple[float, float, float]:
    size = (_GRAY_SIDE, _GRAY_SIDE)
    mask = _subject_mask(*left.shape, core=core)
    region = (cv.warpPerspective(mask, matrix, size) > 250) & (mask > 0)
    minimum_area = .55 * np.count_nonzero(mask) if core else .20 * _GRAY_SIDE ** 2
    if region.sum() < minimum_area:
        return 1.0, 0.0, 0.0
    warped = cv.warpPerspective(left, matrix, size).astype(np.float32)
    right = right.astype(np.float32)
    x, y = warped[region], right[region]
    if min(x.std(), y.std()) < 4:
        return 1.0, 0.0, 0.0
    gain = np.clip(y.std() / max(x.std(), 1), .7, 1.4)
    corrected = np.clip((warped - x.mean()) * gain + y.mean(), 0, 255)
    blur_left = cv.GaussianBlur(corrected, (3, 3), .7)
    blur_right = cv.GaussianBlur(right, (3, 3), .7)
    mae = float(np.abs(blur_left - blur_right)[region].mean() / 255)
    ncc = float(np.corrcoef(blur_left[region], blur_right[region])[0, 1])
    mean_left = cv.GaussianBlur(corrected, (11, 11), 1.5)
    mean_right = cv.GaussianBlur(right, (11, 11), 1.5)
    variance_left = cv.GaussianBlur(corrected * corrected, (11, 11), 1.5) - mean_left * mean_left
    variance_right = cv.GaussianBlur(right * right, (11, 11), 1.5) - mean_right * mean_right
    covariance = cv.GaussianBlur(corrected * right, (11, 11), 1.5) - mean_left * mean_right
    ssim = ((2 * mean_left * mean_right + 6.5025) * (2 * covariance + 58.5225)
            / ((mean_left * mean_left + mean_right * mean_right + 6.5025)
               * (variance_left + variance_right + 58.5225)))
    return mae, ncc, float(ssim[region].mean())


def matches(left: dict, right: dict) -> bool:
    """Accept only spatially distributed, geometrically and visually verified matches."""
    try:
        # Fit one canonical direction. RANSAC's error threshold and resampling
        # must not make the result depend on input or directory iteration order.
        if not isinstance(left, dict) or not isinstance(right, dict):
            return False
        if any(not isinstance(record.get(key), str) for record in [left, right] for key in ["descriptors", "gray"]):
            return False
        if (left["descriptors"], left["gray"]) > (right["descriptors"], right["gray"]):
            left, right = right, left
        points_left, desc_left, image_left = _decode(left)
        points_right, desc_right, image_right = _decode(right)
        if min(len(points_left), len(points_right)) < 20:
            return False
        good = _mutual_matches(desc_left, desc_right)
        if len(good) < 20:
            return False
        source = np.float32([points_left[match.queryIdx] for match in good])
        target = np.float32([points_right[match.trainIdx] for match in good])
        matrix, inlier_mask = cv.findHomography(source, target, cv.RANSAC, .75, maxIters=1200, confidence=.995)
        if matrix is None or inlier_mask is None or not _valid_homography(matrix):
            return False
        inliers = inlier_mask.ravel().astype(bool)
        count, ratio = int(inliers.sum()), float(inliers.mean())
        if count < 20 or ratio < .65:
            return False
        coverage = [cv.contourArea(cv.convexHull(points[inliers])) / (_GRAY_SIDE ** 2) for points in [source, target]]
        if min(coverage) < .05:
            return False
        # A textured shared table can dominate both the fitted homography and
        # the large ellipse's average similarity while the food changes. Make
        # the central subject provide its own correspondences and pixel check.
        def inside_core(points):
            return (((points[:, 0] - .5 * _GRAY_SIDE) / (.22 * _GRAY_SIDE)) ** 2
                    + ((points[:, 1] - .53 * _GRAY_SIDE) / (.24 * _GRAY_SIDE)) ** 2 <= 1)
        core_inliers = inliers & inside_core(source) & inside_core(target)
        if core_inliers.sum() < 8:
            return False
        core_coverage = [cv.contourArea(cv.convexHull(points[core_inliers])) / (_GRAY_SIDE ** 2) for points in [source, target]]
        if min(core_coverage) < .01:
            return False
        forward_pixels = _photometric(image_left, image_right, matrix)
        reverse_pixels = _photometric(image_right, image_left, np.linalg.inv(matrix))
        mae = max(forward_pixels[0], reverse_pixels[0])
        ncc = min(forward_pixels[1], reverse_pixels[1])
        ssim = min(forward_pixels[2], reverse_pixels[2])
        if not np.isfinite([mae, ncc, ssim]).all() or mae > .12:
            return False
        strong_geometry = count >= 60 and ratio >= .75 and min(coverage) >= .12
        if not ((ncc >= .60 and ssim >= .65)
                or (strong_geometry and ncc >= .80 and ssim >= .40)):
            return False
        core_forward = _photometric(image_left, image_right, matrix, core=True)
        core_reverse = _photometric(image_right, image_left, np.linalg.inv(matrix), core=True)
        core_mae = max(core_forward[0], core_reverse[0])
        core_ncc = min(core_forward[1], core_reverse[1])
        core_ssim = min(core_forward[2], core_reverse[2])
        return (bool(np.isfinite([core_mae, core_ncc, core_ssim]).all()) and core_mae <= .12
                and ((core_ncc >= .60 and core_ssim >= .65)
                     or (strong_geometry and core_ncc >= .80 and core_ssim >= .40)))
    except (ValueError, TypeError, OverflowError, binascii.Error, zlib.error, cv.error, np.linalg.LinAlgError):
        return False
