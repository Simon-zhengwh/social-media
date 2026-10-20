from __future__ import annotations

import io
import json

import httpx
import pytest
from PIL import Image

from app.services.toapis import ToAPIsClient, ToAPIsError


def image_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), (200, 100, 50)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_upload_and_submit_payload(settings):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/uploads/images"):
            seen["upload_auth"] = request.headers.get("Authorization")
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"url": "https://files.toapis.xyz/uploads/example.png"},
                },
            )
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"id": "task_1", "status": "queued"})

    client = ToAPIsClient(settings, transport=httpx.MockTransport(handler))
    with_png = settings.storage_path / "probe.png"
    with_png.parent.mkdir(parents=True, exist_ok=True)
    with_png.write_bytes(image_bytes())
    url = client.upload_reference(with_png)
    submitted = client.submit_generation("prompt", [url])
    assert seen["upload_auth"] == "Bearer test-key"
    assert seen["body"]["model"] == settings.toapis_image_model
    assert seen["body"]["size"] == "1:1"
    assert seen["body"]["resolution"] == "1K"
    assert seen["body"]["reference_images"] == [url]
    assert submitted["id"] == "task_1"
    client.close()


def test_wait_for_generation_completes(settings):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(
                200, json={"id": "task_1", "status": "in_progress", "progress": 50}
            )
        return httpx.Response(
            200,
            json={
                "id": "task_1",
                "status": "completed",
                "progress": 100,
                "result": {
                    "data": [{"url": "https://files.toapis.xyz/images/final.png"}]
                },
            },
        )

    client = ToAPIsClient(settings, transport=httpx.MockTransport(handler))
    updates = []
    url = client.wait_for_generation("task_1", on_update=updates.append)
    assert url.endswith("/images/final.png")
    assert [item["status"] for item in updates] == ["in_progress", "completed"]
    client.close()


def test_failed_generation_raises(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "task_1",
                "status": "failed",
                "error": {"code": "generation_failed", "message": "upstream 422"},
            },
        )

    client = ToAPIsClient(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(ToAPIsError) as exc:
        client.wait_for_generation("task_1")
    assert exc.value.code == "generation_failed"
    client.close()


def test_completed_without_url_raises(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "task_1", "status": "completed"})

    client = ToAPIsClient(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(ToAPIsError) as exc:
        client.wait_for_generation("task_1")
    assert exc.value.code == "completed_without_url"
    client.close()


def test_download_rejects_unknown_host(settings):
    client = ToAPIsClient(settings)
    with pytest.raises(ToAPIsError) as exc:
        client.download_result("https://example.com/a.png")
    assert exc.value.code == "unsafe_download"
    client.close()


def test_download_retries_incomplete_stream(settings):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.RemoteProtocolError("peer closed connection")
        return httpx.Response(
            200,
            content=image_bytes(),
            headers={"Content-Type": "image/png"},
        )

    client = ToAPIsClient(settings, transport=httpx.MockTransport(handler))
    data = client.download_result("https://files.toapis.xyz/images/final.png")
    assert data.startswith(b"\x89PNG")
    assert calls["count"] == 2
    client.close()


def test_download_raises_after_retries(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("peer closed connection")

    client = ToAPIsClient(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(ToAPIsError) as exc:
        client.download_result("https://files.toapis.xyz/images/final.png")
    assert exc.value.code == "download_failed"
    client.close()
