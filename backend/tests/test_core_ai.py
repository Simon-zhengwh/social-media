from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import httpx
import mongomock
import pytest
from PIL import Image

from app.services.core_ai import CoreAIClient, CoreAIError
from app.services.image_ref import ImageRef, image_ref_from_asset


def image_bytes(size=(96, 96), color=(180, 90, 50)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def image_file(tmp_path: Path, name: str, color=(180, 90, 50)) -> Path:
    path = tmp_path / name
    path.write_bytes(image_bytes(color=color))
    return path


def transparent_logo_file(tmp_path: Path) -> Path:
    image = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    for x in range(30, 98):
        for y in range(30, 98):
            image.putpixel((x, y), (190, 20, 20, 255))
    path = tmp_path / "logo.png"
    image.save(path, format="PNG")
    return path


def test_generate_image_payload_and_b64_response(settings):
    expected = image_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/images/generations")
        assert request.headers["Authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body["model"] == "gpt-image-2.5-sunburst"
        assert body["size"] == "1024x1024"
        assert body["quality"] == "high"
        assert "input_images" not in body
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(expected).decode()}]},
        )

    client = CoreAIClient(settings, transport=httpx.MockTransport(handler))
    try:
        assert client.generate_image("poster", size="1024x1024") == expected
    finally:
        client.close()


def test_edit_image_sends_multiple_data_urls(settings, tmp_path):
    expected = image_bytes()
    first = image_file(tmp_path, "product.png")
    second = image_file(tmp_path, "logo.png", (200, 20, 20))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/images/edits")
        body = json.loads(request.content)
        assert len(body["input_images"]) == 2
        assert all(
            item["b64Json"].startswith("data:image/jpeg;base64,")
            for item in body["input_images"]
        )
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(expected).decode()}]},
        )

    client = CoreAIClient(settings, transport=httpx.MockTransport(handler))
    try:
        result = client.edit_image(
            "combine",
            [first, second],
            size="1024x1024",
        )
        assert result == expected
    finally:
        client.close()


def test_image_ref_prefers_public_asset_url():
    class PublicStore:
        def local_path(self, key):
            return None

        def presign(self, key, expires):
            assert key == "social-media/prepared/asset_ref.png"
            assert expires > 0
            return "https://example.com/asset_ref.png"

        def download_to_temp(self, key, suffix=""):
            raise AssertionError("public URL should not be downloaded")

    db = mongomock.MongoClient().db
    db.assets.insert_one(
        {
            "_id": "asset_ref",
            "kind": "brand_reference",
            "original_name": "reference-1.jpg",
            "prepared_path": "social-media/prepared/asset_ref.png",
            "sha256": "a" * 64,
        }
    )
    reference = image_ref_from_asset(db, PublicStore(), "asset_ref")
    assert reference.url == "https://example.com/asset_ref.png"
    assert reference.path is None
    assert reference.name == "reference-1.jpg"


def test_edit_image_sends_public_url_without_base64(settings):
    expected = image_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/images/edits")
        body = json.loads(request.content)
        assert body["input_images"] == [{"url": "https://example.com/brand-reference.jpg"}]
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(expected).decode()}]},
        )

    client = CoreAIClient(settings, transport=httpx.MockTransport(handler))
    try:
        reference = ImageRef(
            url="https://example.com/brand-reference.jpg",
            name="reference-1.jpg",
        )
        assert client.edit_image("edit", [reference], size="1024x1024") == expected
    finally:
        client.close()


def test_edit_image_keeps_logo_transparency(settings, tmp_path):
    expected = image_bytes()
    product = image_file(tmp_path, "product.png")
    logo = transparent_logo_file(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["input_images"][0]["b64Json"].startswith("data:image/jpeg;base64,")
        assert body["input_images"][1]["b64Json"].startswith("data:image/png;base64,")
        encoded = body["input_images"][1]["b64Json"].split(",", 1)[1]
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as decoded:
            assert "A" in decoded.getbands()
            assert decoded.convert("RGBA").getchannel("A").getextrema()[0] == 0
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(expected).decode()}]},
        )

    client = CoreAIClient(settings, transport=httpx.MockTransport(handler))
    try:
        assert client.edit_image("logo", [product, logo], size="1024x1024") == expected
    finally:
        client.close()


def test_edit_image_downloads_url(settings, tmp_path, monkeypatch):
    expected = image_bytes()
    reference = image_file(tmp_path, "product.png")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/images/edits"):
            return httpx.Response(
                200,
                json={"data": [{"url": "https://example.com/generated.png"}]},
            )
        assert request.url.path == "/generated.png"
        return httpx.Response(200, content=expected, headers={"Content-Type": "image/png"})

    client = CoreAIClient(settings, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(client, "_validate_public_url", lambda url: None)
    try:
        assert client.edit_image("edit", [reference], size="1024x1024") == expected
    finally:
        client.close()


def test_download_rejects_http_and_private_addresses(settings):
    client = CoreAIClient(settings)
    try:
        with pytest.raises(CoreAIError) as http_exc:
            client.download_image("http://example.com/image.png")
        assert http_exc.value.code == "unsafe_download"
        with pytest.raises(CoreAIError) as private_exc:
            client.download_image("https://127.0.0.1/image.png")
        assert private_exc.value.code == "unsafe_download"
    finally:
        client.close()


def test_core_ai_error_shape(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"errorCode": "BAD_REQUEST", "message": "image prompt is required"},
        )

    client = CoreAIClient(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(CoreAIError) as exc:
            client.generate_image("", size="1024x1024")
        assert exc.value.code == "image_prompt_required"
        with pytest.raises(CoreAIError) as http_exc:
            client.generate_image("poster", size="1024x1024")
        assert http_exc.value.code == "BAD_REQUEST"
        assert "image prompt is required" in http_exc.value.message
    finally:
        client.close()


def test_chat_json_accepts_markdown_fence(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '```json\n{"headline":"周末特惠"}\n```'
                        }
                    }
                ]
            },
        )

    client = CoreAIClient(settings, transport=httpx.MockTransport(handler))
    try:
        assert client.chat_json([{"role": "user", "content": "x"}]) == {
            "headline": "周末特惠"
        }
    finally:
        client.close()


def test_gateway_timeout_is_result_unknown(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = CoreAIClient(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(CoreAIError) as exc:
            client.generate_image("poster", size="1024x1024")
        assert exc.value.code == "gateway_timeout"
        assert "可能已产生费用" in exc.value.message
    finally:
        client.close()


def test_run_agent_triggers_and_reads_completed_output(settings):
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer test-key"
        if request.method == "POST":
            assert request.url.path == "/api/runs/agent/agent-1/trigger"
            assert json.loads(request.content) == {"input": "设计海报"}
            return httpx.Response(202, json={"run_id": "run-1", "status": "RUNNING"})
        return httpx.Response(200, json={"status": "COMPLETED", "output": '{"prompt":"完成"}'})

    client = CoreAIClient(settings, transport=httpx.MockTransport(handler))
    try:
        assert client.run_agent("agent-1", "设计海报", timeout_seconds=10) == '{"prompt":"完成"}'
    finally:
        client.close()
    assert paths == ["/api/runs/agent/agent-1/trigger", "/api/runs/run-1"]


def test_run_agent_persists_run_id_before_polling(settings):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "run-early", "status": "RUNNING"})
        return httpx.Response(200, json={"status": "COMPLETED", "output": "done"})

    client = CoreAIClient(settings, transport=httpx.MockTransport(handler))
    try:
        result = client.run_agent_detail(
            "agent-1",
            "设计海报",
            timeout_seconds=10,
            poll_interval=0,
            on_run_id=seen.append,
        )
    finally:
        client.close()
    assert result.run_id == "run-early"
    assert seen == ["run-early"]


def test_wait_agent_detail_resumes_existing_run_without_trigger(settings):
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "id": "run-existing",
                "status": "COMPLETED",
                "output": "done",
                "artifacts": [
                    {
                        "file_name": "result.png",
                        "content_type": "image/png",
                        "download_url": "https://files.example.com/result.png",
                    }
                ],
            },
        )

    client = CoreAIClient(settings, transport=httpx.MockTransport(handler))
    try:
        result = client.wait_agent_detail(
            "run-existing",
            timeout_seconds=10,
            poll_interval=0,
        )
    finally:
        client.close()
    assert result.run_id == "run-existing"
    assert paths == ["/api/runs/run-existing"]


def test_run_agent_surfaces_failed_run(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "run-2", "status": "RUNNING"})
        return httpx.Response(200, json={"status": "FAILED", "error": "image unavailable"})

    client = CoreAIClient(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(CoreAIError) as exc:
            client.run_agent("agent-1", "设计海报", timeout_seconds=10)
        assert exc.value.code == "agent_run_failed"
        assert "image unavailable" in exc.value.message
    finally:
        client.close()


def test_upload_image_blob_uses_credential_and_put(settings, monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            assert request.url.path == "/api/blob/upload-credential"
            assert request.url.params["content_type"] == "image/png"
            return httpx.Response(
                200,
                json={
                    "upload_url": "https://storage.example.com/upload?sig=1",
                    "blob_url": "https://cdn.example.com/image.png",
                    "container": "static",
                    "blob_name": "ai/uploads/image.png",
                },
            )
        assert request.method == "PUT"
        assert request.headers["x-ms-blob-type"] == "BlockBlob"
        assert request.content == image_bytes()
        return httpx.Response(201)

    client = CoreAIClient(settings, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(client, "_validate_public_url", lambda url: None)
    try:
        assert client.upload_image_blob(image_bytes()) == "https://cdn.example.com/image.png"
    finally:
        client.close()
    assert calls == [
        ("GET", "/api/blob/upload-credential"),
        ("PUT", "/upload"),
    ]


def test_run_agent_detail_returns_artifacts(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.path == "/api/runs/agent/agent-1/trigger"
            assert json.loads(request.content) == {
                "input": "图一 URL: https://example.com/poster.png"
            }
            return httpx.Response(202, json={"run_id": "run-1", "status": "RUNNING"})
        return httpx.Response(
            200,
            json={
                "id": "run-1",
                "status": "COMPLETED",
                "output": "done",
                "artifacts": [
                    {
                        "file_name": "layer.png",
                        "content_type": "image/png",
                        "download_url": "https://files.example.com/layer.png",
                        "title": "Generated image",
                    },
                    {
                        "file_name": "final.png",
                        "content_type": "image/png",
                        "download_url": "https://files.example.com/final.png",
                    },
                ],
            },
        )

    client = CoreAIClient(settings, transport=httpx.MockTransport(handler))
    try:
        result = client.run_agent_detail(
            "agent-1",
            "图一 URL: https://example.com/poster.png",
            timeout_seconds=10,
            poll_interval=0,
        )
    finally:
        client.close()
    assert result.run_id == "run-1"
    assert result.output == "done"
    assert [item.file_name for item in result.artifacts] == ["layer.png", "final.png"]
