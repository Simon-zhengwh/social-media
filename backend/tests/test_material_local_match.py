from __future__ import annotations

import copy
import io
import json

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageEnhance

from app.services import material_local_match as local


def scene(seed=5):
    """Shared table/plate; only the textured central subject changes by seed."""
    rng = np.random.default_rng(seed)
    image = Image.new("RGB", (512, 384), (75, 45, 25))
    draw = ImageDraw.Draw(image)
    for index in range(0, 512, 7):
        draw.line((index, 0, index - 40, 384), fill=(95 + index % 50, 55, 27), width=2)
    draw.ellipse((65, 45, 450, 360), fill=(238, 235, 225))
    noise = rng.integers(0, 256, (250, 330), dtype=np.uint8)
    subject = np.stack([noise, (noise.astype(np.uint16) * 3 // 4 + 35).astype(np.uint8), noise // 3], axis=-1)
    texture = Image.fromarray(subject)
    mask = Image.new("L", texture.size)
    ImageDraw.Draw(mask).ellipse((0, 0, 329, 249), fill=255)
    image.paste(texture, (95, 75), mask)
    return image


def encode(image):
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=92)
    return buffer.getvalue()


def test_crop_scale_and_brightness_change_are_recognized_in_both_directions():
    original = scene()
    changed = original.crop((24, 10, 488, 370)).resize(original.size, Image.Resampling.LANCZOS)
    changed = ImageEnhance.Brightness(changed).enhance(1.08)
    a = local.extract_features(encode(original))
    b = local.extract_features(encode(changed))
    assert local.matches(a, b)
    assert local.matches(b, a)


def test_same_table_and_plate_with_different_subject_does_not_match():
    a = local.extract_features(encode(scene(1)))
    b = local.extract_features(encode(scene(2)))
    assert not local.matches(a, b)


def test_unrelated_images_and_featureless_images_do_not_match():
    a = local.extract_features(encode(scene()))
    blank = local.extract_features(encode(Image.new("RGB", (512, 384), "white")))
    assert not local.matches(a, blank)
    assert not local.matches(blank, blank)


def test_features_are_bounded_json_records():
    record = local.extract_features(encode(scene()))
    serialized = json.dumps(record)
    assert record["version"] == local.VERSION
    assert len(record["points"]) <= 700
    assert len(serialized) < 180_000
    assert all(0 <= value <= 1 for point in record["points"] for value in point)


@pytest.mark.parametrize("change", [
    {"version": "unknown"},
    {"descriptors": "!bad-base64"},
    {"gray": "not-base64"},
    {"points": [[float("nan"), 0]] * 20},
    {"points": [[0.1, 0.2]] * 701},
    {"points": [[0.1, 0.2]] * 20},
    {"points": [[0.1] * 1000]},
    {"points": [["0.1", "0.2"]] * 20},
    {"descriptors": "a" * 200_000},
])
def test_invalid_feature_payloads_are_rejected_safely(change):
    a = local.extract_features(encode(scene()))
    broken = copy.deepcopy(a)
    broken.update(change)
    assert not local.matches(a, broken)
    assert not local.matches(broken, a)


def test_collinear_keypoints_cannot_be_a_valid_geometric_match():
    a = local.extract_features(encode(scene()))
    broken = copy.deepcopy(a)
    broken["points"] = [[index / max(1, len(a["points"])), .5] for index in range(len(a["points"]))]
    assert not local.matches(a, broken)


def test_invalid_image_is_a_bounded_extraction_error():
    with pytest.raises(ValueError):
        local.extract_features(b"not a jpeg")


def test_featureless_tiny_image_is_valid_to_cache_but_cannot_match():
    value = local.extract_features(encode(Image.new("RGB", (1, 1), "white")))
    assert local.valid_features(value)
    assert not local.matches(value, value)


def test_pair_order_cannot_change_a_comparison_result():
    original = scene()
    changed = ImageEnhance.Contrast(original.crop((40, 12, 490, 355)).resize(original.size)).enhance(.85)
    a = local.extract_features(encode(original))
    b = local.extract_features(encode(changed))
    assert local.matches(a, b) == local.matches(b, a)


@pytest.mark.parametrize("side", [220, 260])
def test_shared_textured_background_cannot_outvote_a_changed_central_subject(side):
    background = Image.fromarray(np.random.default_rng(914).integers(25, 180, (384, 512, 3), dtype=np.uint8))
    values = []
    for seed in [1, 2]:
        image = background.copy()
        subject = Image.fromarray(np.random.default_rng(seed).integers(40, 215, (side, side, 3), dtype=np.uint8))
        mask = Image.new("L", (side, side))
        ImageDraw.Draw(mask).ellipse((0, 0, side - 1, side - 1), fill=255)
        image.paste(subject, (256 - side // 2, 192 - side // 2), mask)
        values.append(local.extract_features(encode(image)))
    assert not local.matches(*values)
    assert not local.matches(*reversed(values))


def test_a_shared_small_central_detail_cannot_outvote_changed_subject_pixels():
    background = Image.fromarray(np.random.default_rng(914).integers(25, 180, (384, 512, 3), dtype=np.uint8))
    shared_detail = Image.fromarray(np.random.default_rng(990).integers(0, 256, (64, 96, 3), dtype=np.uint8))
    values = []
    for seed in [1, 2]:
        image = background.copy()
        subject = Image.fromarray(np.random.default_rng(seed).integers(40, 215, (260, 260, 3), dtype=np.uint8))
        mask = Image.new("L", subject.size)
        ImageDraw.Draw(mask).ellipse((0, 0, 259, 259), fill=255)
        image.paste(subject, (126, 62), mask)
        image.paste(shared_detail, (208, 160))
        values.append(local.extract_features(encode(image)))
    assert not local.matches(*values)
    assert not local.matches(*reversed(values))
