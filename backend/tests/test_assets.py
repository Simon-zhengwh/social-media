from __future__ import annotations

import io
from types import SimpleNamespace

import mongomock
import pytest
from fastapi import HTTPException
from PIL import Image

from app.config import Settings
from app.routes.assets import delete_asset
from app.services import storage
from tests.conftest import png_bytes


def test_prepare_upload_preserves_original_and_metadata(settings, store, product_png):
    result = storage.prepare_upload(settings, store, product_png, "dish.png")
    assert result.width == 320
    assert result.height == 240
    assert result.mime_type == "image/png"
    assert store.get_bytes(result.path) == product_png
    assert store.exists(result.prepared_path)


@pytest.mark.parametrize(
    ("image_format", "filename", "expected_mime"),
    [
        ("JPEG", "photo.jpg", "image/jpeg"),
        ("PNG", "photo.png", "image/png"),
        ("WEBP", "photo.webp", "image/webp"),
        ("GIF", "photo.gif", "image/gif"),
        ("BMP", "photo.bmp", "image/bmp"),
        ("TIFF", "photo.tiff", "image/tiff"),
        ("AVIF", "photo.avif", "image/avif"),
        ("HEIF", "photo.heic", "image/heic"),
        ("HEIF", "photo.heif", "image/heif"),
        ("JPEG2000", "photo.jp2", "image/jp2"),
        ("ICO", "photo.ico", "image/x-icon"),
        ("TGA", "photo.tga", "image/x-tga"),
    ],
)
def test_common_image_formats_are_supported(
    settings, store, image_format, filename, expected_mime
):
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (180, 90, 50)).save(buffer, format=image_format)
    result = storage.prepare_upload(
        settings, store, buffer.getvalue(), filename
    )
    assert result.mime_type == expected_mime
    assert result.prepared_path.endswith(".png")
    assert storage.open_image(store, result.prepared_path).mode == "RGB"


@pytest.mark.parametrize("trigger", ["bytes", "pixels"])
def test_oversized_upload_is_downscaled_instead_of_rejected(settings, store, trigger):
    settings.image_compress_threshold_bytes = 1 if trigger == "bytes" else 100_000_000
    settings.image_downscale_pixel_threshold = (
        1_000_000 if trigger == "pixels" else 100_000_000
    )
    settings.max_image_pixels = 20_000_000
    buffer = io.BytesIO()
    Image.new("RGB", (2400, 1800), (180, 90, 50)).save(buffer, format="JPEG")

    result = storage.prepare_upload(
        settings, store, buffer.getvalue(), "large-photo.jpg"
    )

    assert result.width == 1800
    assert result.height == 1350
    assert result.prepared_path.endswith(".png")


def test_safety_pixel_limit_still_applies(settings, store, product_png):
    settings.max_image_pixels = 1_000
    with pytest.raises(storage.StorageError) as exc:
        storage.prepare_upload(settings, store, product_png, "dish.png")
    assert exc.value.code == "image_too_large"
    assert "安全上限" in str(exc.value)


def test_logo_keeps_transparency(settings, store, logo_png):
    result = storage.prepare_upload(settings, store, logo_png, "logo.png")
    image = storage.open_image(store, result.prepared_path)
    assert image.mode == "RGBA"
    assert image.getpixel((0, 0))[3] == 0
    assert image.getpixel((64, 64))[3] == 255


def test_corrupt_image_is_rejected(settings, store):
    with pytest.raises(storage.StorageError) as exc:
        storage.prepare_upload(settings, store, b"not an image", "bad.png")
    assert exc.value.code == "invalid_image"


def test_default_upload_limit_is_20_mb():
    assert Settings.model_fields["max_upload_bytes"].default == 20_000_000


def test_size_limit_is_enforced(settings, store, product_png):
    settings.max_upload_bytes = 10
    with pytest.raises(storage.StorageError) as exc:
        storage.prepare_upload(settings, store, product_png, "dish.png")
    assert exc.value.code == "file_too_large"
    assert "MB 限制" in str(exc.value)


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
        settings, store, buffer.getvalue(), "rotated.jpg"
    )
    assert (result.width, result.height) == (60, 120)


def test_fully_transparent_image_is_rejected(settings, store):
    image = Image.new("RGBA", (128, 128), (255, 255, 255, 1))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    with pytest.raises(storage.StorageError) as exc:
        storage.prepare_upload(settings, store, buffer.getvalue(), "logo.png")
    assert exc.value.code == "blank_asset"


def test_local_object_store_delete(store):
    store.put_bytes("probe/delete.txt", b"x")
    assert store.exists("probe/delete.txt")
    store.delete("probe/delete.txt")
    assert not store.exists("probe/delete.txt")


def test_delete_asset_can_force_remove_referenced_asset(settings, store, product_png):
    prepared = storage.prepare_upload(settings, store, product_png, "dish.png")
    db = mongomock.MongoClient().db
    db.assets.insert_one(
        {
            "_id": prepared.id,
            "source": "upload",
            "path": prepared.path,
            "prepared_path": prepared.prepared_path,
        }
    )
    db.whole_tasks.insert_one(
        {"_id": "wtask_1", "input": {"product_asset_ids": [prepared.id]}}
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db=db, store=store))
    )

    with pytest.raises(HTTPException) as exc:
        delete_asset(prepared.id, request)
    assert exc.value.status_code == 409

    response = delete_asset(prepared.id, request, force=True)

    assert response.status_code == 204
    assert db.assets.find_one({"_id": prepared.id}) is None
    assert not store.exists(prepared.path)
    assert not store.exists(prepared.prepared_path)


def test_delete_asset_is_blocked_when_whole_image_task_uses_it(
    settings, store, product_png
):
    prepared = storage.prepare_upload(settings, store, product_png, "dish.png")
    db = mongomock.MongoClient().db
    db.assets.insert_one(
        {
            "_id": prepared.id,
            "source": "upload",
            "path": prepared.path,
            "prepared_path": prepared.prepared_path,
        }
    )
    db.whole_tasks.insert_one(
        {"_id": "wtask_1", "input": {"product_asset_ids": [prepared.id]}}
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db=db, store=store))
    )

    with pytest.raises(HTTPException) as exc:
        delete_asset(prepared.id, request)

    assert exc.value.status_code == 409


def test_delete_asset_is_blocked_when_brand_logo_uses_it(settings, store, logo_png):
    prepared = storage.prepare_upload(settings, store, logo_png, "logo.png")
    db = mongomock.MongoClient().db
    db.assets.insert_one(
        {
            "_id": prepared.id,
            "source": "upload",
            "path": prepared.path,
            "prepared_path": prepared.prepared_path,
        }
    )
    db.brands.insert_one({"_id": "brand", "logo_asset_ids": [prepared.id]})
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db=db, store=store))
    )

    with pytest.raises(HTTPException) as exc:
        delete_asset(prepared.id, request)

    assert exc.value.status_code == 409
