from __future__ import annotations

import httpx
import mongomock
import pytest

from app.config import Settings
from app.services.image_host import ImageHostClient, ImageHostError, upload_asset
from app.services.object_store import LocalObjectStore


def settings_with_host(tmp_path):
    return Settings(
        storage_dir=str(tmp_path / "storage"),
        image_host_base_url="https://image.example:5002",
        image_host_upload_token="secret-token",
        image_host_public_base_url="https://image.example/img",
    )


def test_upload_uses_raw_body_auth_and_443_public_url(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        captured["filename"] = request.headers["x-filename"]
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={
                "url": "https://image.example:5002/f/2026/10/26/hash.png",
                "path": "2026/10/26/hash.png",
                "size": 4,
                "sha256": "digest",
            },
        )

    settings = settings_with_host(tmp_path)
    client = ImageHostClient(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.upload("满座 01.png", b"data")

    assert captured["url"] == "https://image.example:5002/api/upload"
    assert captured["authorization"] == "Bearer secret-token"
    assert captured["filename"] == "%E6%BB%A1%E5%BA%A7%2001.png"
    assert captured["body"] == b"data"
    assert result.url == "https://image.example/img/f/2026/10/26/hash.png"
    assert result.path == "2026/10/26/hash.png"


def test_asset_url_is_cached_after_first_upload(tmp_path):
    settings = settings_with_host(tmp_path)
    store = LocalObjectStore(settings.storage_path / "objects")
    key = "social-media/prepared/asset_test.png"
    store.put_bytes(key, b"image-data")
    db = mongomock.MongoClient().db
    db.assets.insert_one(
        {
            "_id": "asset_test",
            "kind": "product",
            "original_name": "菜品.png",
            "prepared_path": key,
        }
    )

    class RecordingHost:
        def __init__(self):
            self.calls = 0

        def upload(self, filename, data):
            self.calls += 1
            assert filename == "asset_test.png"
            assert data == b"image-data"
            return type(
                "Upload",
                (),
                {"url": "https://image.example/img/f/hash.png", "path": "f/hash.png", "sha256": "digest"},
            )()

    host = RecordingHost()
    assert upload_asset(db, store, settings, "asset_test", client=host) == "https://image.example/img/f/hash.png"
    assert upload_asset(db, store, settings, "asset_test", client=host) == "https://image.example/img/f/hash.png"
    assert host.calls == 1
    assert db.assets.find_one({"_id": "asset_test"})["image_host_url"].endswith("hash.png")


def test_quota_error_is_mapped(tmp_path):
    settings = settings_with_host(tmp_path)
    client = ImageHostClient(
        settings,
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(507, json={"error": "full"}))
        ),
    )
    with pytest.raises(ImageHostError) as exc:
        client.upload("dish.png", b"data")
    assert exc.value.code == "image_host_quota"
    assert "容量" in exc.value.message
