from __future__ import annotations

from app.services.object_store import CosObjectStore


class FailingCosClient:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def put_object(self, **kwargs):
        self.writes.append(kwargs["Key"])
        raise RuntimeError("COS unavailable")

    def get_object(self, **kwargs):
        raise RuntimeError("COS unavailable")

    def head_object(self, **kwargs):
        raise RuntimeError("COS unavailable")


def test_cos_failure_keeps_and_serves_local_mirror(settings):
    settings.cos_bucket = "test-bucket"
    settings.cos_region = "test-region"
    settings.cos_secret_id = "test-id"
    settings.cos_secret_key = "test-key"
    client = FailingCosClient()
    store = CosObjectStore(settings, client=client)
    key = "social-media/prepared/asset_test.png"

    store.put_bytes(key, b"image-bytes", "image/png")

    assert client.writes == [key]
    assert store.get_bytes(key) == b"image-bytes"
    assert store.local_path(key) is not None
    assert store.local_path(key).read_bytes() == b"image-bytes"
