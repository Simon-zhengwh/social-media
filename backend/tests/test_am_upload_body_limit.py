from __future__ import annotations

import base64
import io
import json
import random
from types import SimpleNamespace

import httpx
import mongomock
import pytest
from fastapi import UploadFile
from PIL import Image
from starlette.datastructures import Headers

from app.config import Settings
from app.routes import assets as asset_routes
from app.services import storage
from app.services.am_client import AMClient, AMClientError
from app.services.scoped_db import ScopedDatabase


def _client(handler) -> AMClient:
    settings = Settings(am_base_url="https://am.example", am_timeout_seconds=3)
    return AMClient(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
    )


def _request(db, settings, *, am, store):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db=db, settings=settings, am=am, store=store)),
        state=SimpleNamespace(am_cookie="AMSessionId=abc", merchant_id="m1"),
    )


def _upload(data: bytes, filename: str = "孜然羊肉.JPG") -> UploadFile:
    return UploadFile(
        file=io.BytesIO(data),
        filename=filename,
        headers=Headers({"content-type": "image/jpeg"}),
    )


def _noise_jpeg(width: int = 480, height: int = 360) -> bytes:
    """A photo-like image that JPEG cannot shrink to nothing, so the body budget matters."""
    generator = random.Random(7)
    image = Image.new("RGB", (width, height))
    image.putdata([(generator.randrange(256), generator.randrange(256), generator.randrange(256)) for _ in range(width * height)])
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def test_base64_body_size_matches_the_encoder():
    for size in (0, 1, 2, 3, 7, 1024, 1_000_001):
        encoded = base64.b64encode(b"x" * size)
        assert storage.base64_body_size(size) == len(encoded)


def test_upload_within_the_budget_is_sent_byte_for_byte(product_png):
    fitted = storage.fit_upload_to_body(product_png, 9_500_000, "dish.png", "image/png")
    assert fitted.recompressed is False
    assert fitted.data == product_png
    assert fitted.filename == "dish.png"
    assert fitted.mime_type == "image/png"


def test_oversized_upload_is_recompressed_before_it_reaches_am(settings, store):
    """A 16 MB original answers a bare ingress 400 today; the library copy must fit the body limit."""
    settings.am_upload_max_body_bytes = 200_000
    bodies: list[dict] = []

    def handler(request: httpx.Request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"media_library": {"id": "m-9", "name": "孜然羊肉.jpg", "media_library_format": "JPEG", "tags": []}},
        )

    am = _client(handler)
    db = ScopedDatabase(mongomock.MongoClient().db, "m1")
    original = _noise_jpeg()
    assert storage.base64_body_size(len(original)) > settings.am_upload_max_body_bytes

    uploaded = asset_routes.upload_asset(_request(db, settings, am=am, store=store), _upload(original))

    assert len(bodies) == 1
    sent = bodies[0]
    assert storage.base64_body_size(len(base64.b64decode(sent["file_base64"]))) <= settings.am_upload_max_body_bytes
    decoded = base64.b64decode(sent["file_base64"])
    assert decoded[:2] == b"\xff\xd8", "re-encoded copy must be a real JPEG"
    assert sent["media_library_format"] == "JPEG"
    assert sent["filename"] == "孜然羊肉.jpg"

    document = db.assets.find_one({"_id": uploaded["id"]})
    assert document["am_media_library_id"] == "m-9"
    assert document["am_recompressed"] is True
    assert document["am_upload_bytes"] == len(decoded)
    # The local original and the prepared copy keep the uploaded bytes and pixels.
    assert document["bytes"] == len(original)


def test_upload_that_cannot_fit_is_reported_before_calling_am():
    with pytest.raises(storage.StorageError) as exc:
        storage.fit_upload_to_body(_noise_jpeg(2048, 2048), 1_500, "big.jpg", "image/jpeg")
    assert exc.value.code == "file_too_large"
    assert "请求上限" in exc.value.message


def test_transparent_png_is_flattened_on_white():
    generator = random.Random(11)
    image = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
    for x in range(40, 260):
        for y in range(40, 260):
            image.putpixel((x, y), (generator.randrange(256), generator.randrange(256), generator.randrange(256), 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    logo = buffer.getvalue()

    fitted = storage.fit_upload_to_body(logo, 200_000, "logo.png", "image/png")

    assert fitted.recompressed is True
    assert fitted.filename == "logo.jpg"
    assert fitted.mime_type == "image/jpeg"
    with Image.open(io.BytesIO(fitted.data)) as converted:
        assert converted.mode == "RGB"
        # Transparent pixels are composited on white, not left black.
        assert min(converted.getpixel((5, 5))) >= 250


def test_container_am_cannot_store_is_sent_as_jpeg(settings, store):
    """AM keeps only JPEG/PNG/GIF, so a WEBP original must not be labelled PNG."""
    bodies: list[dict] = []

    def handler(request: httpx.Request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, json={"media_library": {"id": "m-3", "name": "dish.jpg", "media_library_format": "JPEG", "tags": []}}
        )

    am = _client(handler)
    db = ScopedDatabase(mongomock.MongoClient().db, "m1")
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (30, 160, 90)).save(buffer, format="WEBP")

    asset_routes.upload_asset(_request(db, settings, am=am, store=store), _upload(buffer.getvalue(), "dish.webp"))

    sent = bodies[0]
    assert sent["media_library_format"] == "JPEG"
    assert sent["filename"] == "dish.jpg"
    assert base64.b64decode(sent["file_base64"])[:2] == b"\xff\xd8"


def test_am_client_refuses_a_container_it_cannot_store():
    am = _client(lambda request: httpx.Response(200, json={"media_library": {"id": "m-1"}}))
    with pytest.raises(AMClientError) as exc:
        am.upload_media("AMSessionId=abc", "m1", filename="dish.webp", data=b"x", mime_type="image/webp")
    assert exc.value.code == "am_unsupported_media_format"
    assert exc.value.status_code == 415
