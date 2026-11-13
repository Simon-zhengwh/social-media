from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from app.services import material_similarity as similarity


def _bytes(image: Image.Image, **save_options) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", **save_options)
    return buffer.getvalue()


def _photo(color="tomato", *, alternative=False) -> Image.Image:
    image = Image.new("RGB", (320, 240), "#b59b76")
    draw = ImageDraw.Draw(image)
    draw.ellipse((30, 20, 290, 225), fill="#e8e8df", outline="#777366", width=4)
    if alternative:
        draw.rectangle((70, 75, 150, 185), fill=color)
        draw.rectangle((170, 75, 250, 185), fill=color)
    else:
        draw.ellipse((70, 65, 250, 190), fill=color)
        draw.line((95, 100, 230, 155), fill="#773b20", width=10)
        draw.ellipse((125, 115, 155, 145), fill="#efe096")
    return image


def _shared_background_photo(*, color="#9b4b20", rearranged=False) -> Image.Image:
    """Small food on a large plate: the surroundings dominate global hashes."""
    image = Image.new("RGB", (512, 384), "#a98a64")
    draw = ImageDraw.Draw(image)
    for y in range(0, 384, 24):
        draw.line((0, y, 511, y), fill="#8a7153", width=2)
    draw.ellipse((42, 22, 470, 362), fill="#eeece7", outline="#c4c2bd", width=5)
    draw.ellipse((180, 145, 330, 245), fill=color)
    if rearranged:
        draw.rectangle((220, 150, 250, 230), fill="#d7c676")
        draw.rectangle((270, 150, 300, 230), fill="#d7c676")
    else:
        draw.rectangle((195, 165, 310, 185), fill="#d7c676")
        draw.rectangle((195, 205, 310, 225), fill="#d7c676")
    return image


def _item(identifier, *, phash="0000000000000000", dhash=None,
          color=None, quality=0.5, sha256=None, path=None):
    return {
        "id": identifier,
        "sha256": sha256 or f"sha-{identifier}",
        "relative_path": path or f"folder/{identifier}.jpg",
        "features": {
            "version": similarity.VERSION,
            "phash": phash,
            "dhash": dhash if dhash is not None else phash,
            "color": color if color is not None else [1.0, 0.0],
            "subject_phash": phash,
            "subject_dhash": dhash if dhash is not None else phash,
            "subject_color": color if color is not None else [1.0, 0.0],
            "quality": quality,
            "width": 320,
            "height": 240,
        },
    }


def _groups(groups):
    return [[member["id"] for member in group] for group in groups]


def test_geometry_verification_can_recover_changed_framing_without_chaining(monkeypatch):
    from app.services import material_local_match as local
    items=[_item("a"),_item("b"),_item("c")]
    for index,item in enumerate(items):
        item["features"]["subject_phash"]=f"{(1 << (16*index)) - 1:016x}"
        item["features"]["alignment"]={"id":item["id"]}
    # A/B and B/C are geometrically alike, A/C is not: do not chain them.
    monkeypatch.setattr(local,"matches",lambda a,b:{a["id"],b["id"]} in [{"a","b"},{"b","c"}])
    assert _groups(similarity.cluster_files(items))==[["a","b"],["c"]]


def test_geometry_fallback_still_rejects_coarsely_different_photos(monkeypatch):
    from app.services import material_local_match as local
    a=_item("a");b=_item("b",phash="ffffffffffffffff")
    for row in [a,b]:row["features"]["alignment"]={"id":row["id"]}
    monkeypatch.setattr(local,"matches",lambda *args:pytest.fail("coarse gate should reject before expensive matching"))
    assert _groups(similarity.cluster_files([a,b]))==[["a"],["b"]]


def test_scaled_slightly_brighter_photo_remains_in_one_group():
    original = _photo()
    resized = ImageEnhance.Brightness(original.resize((160, 120))).enhance(1.06)
    features = [similarity.extract_features(_bytes(image)) for image in (original, resized)]
    for feature in features:
        assert feature["version"] == "visual-hash-v2"
        assert len(feature["phash"]) == len(feature["dhash"]) == 16
        assert len(feature["subject_phash"]) == len(feature["subject_dhash"]) == 16
        assert sum(feature["subject_color"]) == pytest.approx(1)
        assert 0 <= feature["quality"] <= 1
        assert all(isinstance(value, float) for value in feature["color"])
    assert features[0]["width"] == 320
    assert features[1]["height"] == 120
    items = [_item("a"), _item("b")]
    for item, feature in zip(items, features):
        item["features"] = feature
    assert _groups(similarity.cluster_files(items)) == [["a", "b"]]


def test_different_subject_colors_or_composition_do_not_merge():
    photos = [_photo(), _photo("#12693e"), _photo(alternative=True)]
    items = [_item(str(index)) for index in range(len(photos))]
    for item, photo in zip(items, photos):
        item["features"] = similarity.extract_features(_bytes(photo))
    assert _groups(similarity.cluster_files(items)) == [["0"], ["1"], ["2"]]


@pytest.mark.parametrize("other", [
    {"color": "#32783b"},
    {"rearranged": True},
])
def test_shared_plate_and_table_do_not_merge_different_central_food(other):
    items = [_item("a"), _item("b")]
    for item, photo in zip(items, (_shared_background_photo(), _shared_background_photo(**other))):
        item["features"] = similarity.extract_features(_bytes(photo))
    assert _groups(similarity.cluster_files(items)) == [["a"], ["b"]]


def test_small_subject_on_shared_background_keeps_brightness_and_scale_variants_together():
    original = _shared_background_photo()
    variant = ImageEnhance.Brightness(original.resize((256, 192))).enhance(1.06)
    items = [_item("a"), _item("b")]
    for item, photo in zip(items, (original, variant)):
        item["features"] = similarity.extract_features(_bytes(photo))
    assert _groups(similarity.cluster_files(items)) == [["a", "b"]]


def test_color_separates_flat_images_with_identical_spatial_hashes():
    items = [_item("red"), _item("green")]
    for item, color in zip(items, ("red", "green")):
        item["features"] = similarity.extract_features(_bytes(Image.new("RGB", (64, 64), color)))
    assert len(similarity.cluster_files(items)) == 2


def test_complete_link_rejects_similarity_chaining_and_is_order_independent():
    a = _item("a", phash="0000000000000000")
    b = _item("b", phash="000000000000000f")
    c = _item("c", phash="00000000000000ff")
    expected = [["a", "b"], ["c"]]
    assert _groups(similarity.cluster_files([a, b, c], max_phash=4, max_dhash=4)) == expected
    assert _groups(similarity.cluster_files([c, a, b], max_phash=4, max_dhash=4)) == expected


def test_exact_content_duplicates_group_without_losing_independent_ids():
    a = _item("a", sha256="same")
    b = _item("b", sha256="same", phash="ffffffffffffffff", color=[0.0, 1.0])
    assert _groups(similarity.cluster_files([b, a])) == [["a", "b"]]


def test_matching_is_global_and_never_uses_path_adjacency_as_evidence():
    a = _item("a", path="first/IMG_0001.JPG")
    b = _item("b", path="second/IMG_0199.JPG")
    unrelated = _item("c", path="first/IMG_0002.JPG", phash="ffffffffffffffff")
    assert _groups(similarity.cluster_files([b, unrelated, a])) == [["a", "b"], ["c"]]


def test_stale_feature_versions_are_not_used_for_near_matching():
    a, b = _item("a"), _item("b")
    b["features"]["version"] = "obsolete"
    assert _groups(similarity.cluster_files([a, b])) == [["a"], ["b"]]


@pytest.mark.parametrize("missing", ["subject_phash", "subject_dhash", "subject_color"])
def test_missing_subject_features_cannot_bypass_conservative_matching(missing):
    a, b = _item("a"), _item("b")
    del b["features"][missing]
    assert _groups(similarity.cluster_files([a, b])) == [["a"], ["b"]]


@pytest.mark.parametrize("changed", [
    {"subject_phash": "ffffffffffffffff"},
    {"subject_dhash": "ffffffffffffffff"},
    {"subject_color": [0.0, 1.0]},
])
def test_global_similarity_requires_each_subject_check_as_well(changed):
    a, b = _item("a"), _item("b")
    b["features"].update(changed)
    assert _groups(similarity.cluster_files([a, b])) == [["a"], ["b"]]


@pytest.mark.parametrize("phash_bits,dhash_bits,color_distance", [
    (12, 15, 0.107),  # L1: same background, visually different food.
    (16, 15, 0.225),  # H1
    (16, 18, 0.158),  # H2
    (16, 17, 0.148),  # H3
    (12, 14, 0.210),  # Color must also reject a shape-compatible food swap.
])
def test_measured_shared_background_false_pairs_are_split(phash_bits, dhash_bits, color_distance):
    # Four recorded subject distances from the independent local visual review,
    # plus a color-only boundary regression.
    # Keep global features identical to isolate the subject acceptance policy.
    a, b = _item("a"), _item("b")
    first_bin = (1 - color_distance ** 2) ** 2
    b["features"].update(
        subject_phash=f"{(1 << phash_bits) - 1:016x}",
        subject_dhash=f"{(1 << dhash_bits) - 1:016x}",
        subject_color=[first_bin, 1 - first_bin],
    )
    measured_color = similarity.color_distance(a["features"]["subject_color"], b["features"]["subject_color"])
    assert measured_color == pytest.approx(color_distance)
    assert _groups(similarity.cluster_files([a, b])) == [["a"], ["b"]]


def test_tighter_subject_policy_still_keeps_near_frames_at_hash_limits_together():
    a, b = _item("a"), _item("b")
    b["features"].update(subject_phash="0000000000000fff", subject_dhash="0000000000003fff")
    assert _groups(similarity.cluster_files([a, b])) == [["a", "b"]]


def test_grouping_policy_has_a_version_independent_of_cached_features():
    assert similarity.VERSION == "visual-hash-v2"
    assert similarity.GROUPING_VERSION == "complete-link-geometry-v5"


def test_representative_prefers_quality_near_group_center_and_then_cache():
    members = [
        _item("a", phash="000000000000000f", quality=0.4),
        _item("b", phash="0000000000000003", quality=0.8),
        _item("c", phash="0000000000000000", quality=0.6),
    ]
    assert similarity.choose_representatives(members) == ["b"]
    assert similarity.choose_representatives(members[::-1]) == ["b"]
    assert similarity.choose_representatives(members, cached_ids={"a", "missing"}) == ["a"]


def test_second_representative_is_diverse_and_count_is_bounded():
    members = [
        _item("a", quality=1.0),
        _item("b", phash="0000000000000001", quality=0.8),
        _item("c", phash="00000000000000ff", quality=0.5),
        _item("d", phash="000000000000000f", quality=0.6),
    ]
    assert similarity.choose_representatives(members, count=2, cached_ids={"a"}) == ["a", "c"]
    assert len(similarity.choose_representatives(members, count=99)) == 2
    assert similarity.choose_representatives(members, count=0) == []
    assert similarity.choose_representatives([], count=2) == []


def test_second_representative_avoids_exact_and_visually_identical_duplicates():
    members = [
        _item("a", sha256="same"),
        _item("b", sha256="same", phash="00000000000000ff"),
        _item("c"),
        _item("d", phash="0000000000000001"),
    ]
    assert similarity.choose_representatives(members, count=2, cached_ids={"a"}) == ["a"]


def test_second_representative_prefers_existing_valid_result():
    members = [
        _item("a", quality=1.0),
        _item("b", phash="000000000000000f", quality=0.5),
        _item("c", phash="00000000000000ff", quality=0.6),
    ]
    assert similarity.choose_representatives(members, count=2, cached_ids={"a", "b"}) == ["a", "b"]


def test_sharper_photo_has_higher_quality_than_blurred_copy():
    original = _photo()
    sharp = similarity.extract_features(_bytes(original))
    blurred = similarity.extract_features(_bytes(original.filter(ImageFilter.GaussianBlur(8))))
    assert sharp["quality"] > blurred["quality"]


def test_overexposed_photo_has_lower_quality_than_normal_copy():
    original = _photo()
    normal = similarity.extract_features(_bytes(original))
    overexposed = similarity.extract_features(_bytes(ImageEnhance.Brightness(original).enhance(2)))
    assert normal["quality"] > overexposed["quality"]


def test_exif_orientation_is_applied_to_hashes_and_dimensions():
    original = _photo()
    exif = Image.Exif()
    exif[274] = 6
    encoded = original.transpose(Image.Transpose.ROTATE_90)
    normal = similarity.extract_features(_bytes(original))
    oriented = similarity.extract_features(_bytes(encoded, exif=exif))
    assert (oriented["width"], oriented["height"]) == (320, 240)
    assert normal["phash"] == oriented["phash"]
    assert normal["dhash"] == oriented["dhash"]


@pytest.mark.parametrize("data", [b"", b"not an image", b"\x89PNG\r\n\x1a\n"])
def test_invalid_image_is_rejected(data):
    with pytest.raises(ValueError, match="image"):
        similarity.extract_features(data)


def test_image_pixel_limit_is_checked_before_decode(monkeypatch):
    data = _bytes(Image.new("RGB", (11, 10)))
    monkeypatch.setattr(similarity, "MAX_IMAGE_PIXELS", 100)
    monkeypatch.setattr(Image.Image, "load", lambda *args, **kwargs: pytest.fail("decoded oversized image"))
    with pytest.raises(ValueError, match="image.*large"):
        similarity.extract_features(data)


def test_encoded_byte_limit_is_checked_before_open(monkeypatch):
    monkeypatch.setattr(similarity, "MAX_IMAGE_BYTES", 2)
    monkeypatch.setattr(Image, "open", lambda *args, **kwargs: pytest.fail("opened oversized input"))
    with pytest.raises(ValueError, match="image.*large"):
        similarity.extract_features(b"123")


def test_tiny_transparent_photo_is_supported():
    feature = similarity.extract_features(_bytes(Image.new("RGBA", (1, 1), (0, 0, 0, 0))))
    assert (feature["width"], feature["height"]) == (1, 1)
    assert 0 <= feature["quality"] <= 1
    assert sum(feature["color"]) == pytest.approx(1)
