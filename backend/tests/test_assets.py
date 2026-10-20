from __future__ import annotations

import io

import pytest
from PIL import Image

from app.services import storage
from tests.conftest import png_bytes


def test_prepare_upload_preserves_original_and_metadata(settings, store, product_png):
    result = storage.prepare_upload(settings, store, product_png, "dish.png", "product")
    assert result.width == 320
    assert result.height == 240
    assert result.kind == "product"
    assert result.mime_type == "image/png"
    assert store.get_bytes(result.path) == product_png
    assert store.exists(result.prepared_path)


def test_logo_keeps_transparency(settings, store, logo_png):
    result = storage.prepare_upload(settings, store, logo_png, "logo.png", "logo")
    image = storage.open_image(store, result.prepared_path)
    assert image.mode == "RGBA"
    assert image.getpixel((0, 0))[3] == 0
    assert image.getpixel((64, 64))[3] == 255


def test_corrupt_image_is_rejected(settings, store):
    with pytest.raises(storage.StorageError) as exc:
        storage.prepare_upload(settings, store, b"not an image", "bad.png", "product")
    assert exc.value.code == "invalid_image"


def test_invalid_kind_is_rejected(settings, store, product_png):
    with pytest.raises(storage.StorageError) as exc:
        storage.prepare_upload(settings, store, product_png, "dish.png", "background")
    assert exc.value.code == "invalid_kind"


def test_size_limit_is_enforced(settings, store, product_png):
    settings.max_upload_bytes = 10
    with pytest.raises(storage.StorageError) as exc:
        storage.prepare_upload(settings, store, product_png, "dish.png", "product")
    assert exc.value.code == "file_too_large"


def test_path_traversal_is_rejected(settings):
    with pytest.raises(storage.StorageError) as exc:
        storage.resolve_storage_path(settings, "../outside.png")
    assert exc.value.code == "invalid_path"


def test_exif_orientation_is_applied(settings, store):
    image = Image.new("RGB", (120, 60), (20, 120, 60))
    exif = Image.Exif()
    exif[274] = 6
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    result = storage.prepare_upload(
        settings, store, buffer.getvalue(), "rotated.jpg", "product"
    )
    assert (result.width, result.height) == (60, 120)


def test_blank_logo_is_rejected(settings, store):
    image = Image.new("RGBA", (128, 128), (255, 255, 255, 1))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    with pytest.raises(storage.StorageError) as exc:
        storage.prepare_upload(settings, store, buffer.getvalue(), "logo.png", "logo")
    assert exc.value.code == "blank_logo"


def test_local_object_store_delete(store):
    store.put_bytes("probe/delete.txt", b"x")
    assert store.exists("probe/delete.txt")
    store.delete("probe/delete.txt")
    assert not store.exists("probe/delete.txt")
