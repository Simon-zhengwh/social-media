from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.config import Settings
from app.services import image_provider
from app.services.core_ai import CoreAIError
from app.services.image_ref import ImageRef
from app.services.minimax_image import MiniMaxImageClient, minimax_size


def png_bytes(width: int = 320, height: int = 240) -> bytes:
    image = Image.new("RGB", (width, height), (120, 60, 30))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def settings(tmp_path: Path) -> Settings:
    return Settings(
        storage_dir=str(tmp_path / "storage"),
        minimax_image_api_key="mm-key",
        minimax_image_base_url="https://api.minimax.test/v1",
        minimax_image_timeout_seconds=5,
        am_media_storage_service_key="",
    )


def client(config: Settings, handler, **kwargs) -> MiniMaxImageClient:
    return MiniMaxImageClient(
        config,
        transport=httpx.MockTransport(handler),
        validate_url=lambda url: None,
        **kwargs,
    )


def _download_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"Content-Type": "image/png"}, content=png_bytes())


def test_generate_posts_documented_payload(tmp_path):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen.append(json.loads(request.content))
            assert request.headers["Authorization"] == "Bearer mm-key"
            assert str(request.url) == "https://api.minimax.test/v1/images/generations"
            return httpx.Response(
                200,
                json={
                    "output_format": "png",
                    "data": [{"url": "https://cdn.test/out.png", "size": "1024x1024"}],
                },
            )
        return _download_ok(request)

    data = client(settings(tmp_path), handler).generate_image(
        "夏日冰美式海报", size="1024x1024", model="minimax-h3-image"
    )

    assert data.startswith(b"\x89PNG")
    payload = seen[0]
    assert payload["model"] == "MiniMax-H3-Image"
    assert payload["size"] == "1024x1024"
    assert payload["n"] == 1
    assert payload["response_format"] == "url"
    assert payload["output_format"] == "png"
    # Our prompts carry the fidelity rules; the enhancer would rewrite them.
    assert payload["disable_prompt_enhanced"] is True


def test_highspeed_model_is_rejected_until_the_team_asks_for_it(tmp_path):
    with pytest.raises(CoreAIError) as error:
        image_provider.image_client(
            settings(tmp_path), object(), "minimax-h3-image-highspeed"
        )
    assert error.value.code == "invalid_image_model"


def test_edit_sends_references_as_data_urls_in_order(tmp_path):
    first = tmp_path / "product.png"
    first.write_bytes(png_bytes(400, 300))
    second = tmp_path / "logo.png"
    second.write_bytes(png_bytes(128, 128))
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen.append(json.loads(request.content))
            assert str(request.url) == "https://api.minimax.test/v1/images/edits"
            return httpx.Response(
                200, json={"data": [{"url": "https://cdn.test/edit.png"}]}
            )
        return _download_ok(request)

    client(settings(tmp_path), handler).edit_image(
        "把产品换成第二张",
        [ImageRef(path=first), ImageRef(path=second)],
        size="1024x1536",
        model="minimax-h3-image",
    )

    images = seen[0]["images"]
    assert len(images) == 2
    assert all(item["image_url"].startswith("data:image/") for item in images)
    decoded = base64.b64decode(images[0]["image_url"].split(",", 1)[1])
    with Image.open(io.BytesIO(decoded)) as encoded:
        assert encoded.size == (400, 300)


def test_edit_rejects_masks_because_minimax_has_no_mask(tmp_path):
    with pytest.raises(CoreAIError) as error:
        client(settings(tmp_path), _download_ok).edit_image(
            "局部修改",
            [ImageRef(path=tmp_path / "x.png")],
            size="1024x1024",
            mask=tmp_path / "mask.png",
        )
    assert error.value.code == "unsupported_mask"


def test_per_image_failure_returns_actionable_message(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "error": {
                            "code": "1027",
                            "message": "output image failed content safety check",
                        }
                    }
                ]
            },
        )

    with pytest.raises(CoreAIError) as error:
        client(settings(tmp_path), handler).generate_image("海报", size="1024x1024")
    assert "content safety" in error.value.message


def test_http_errors_map_to_operator_readable_codes(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"base_resp": {"status_msg": "余额不足"}})

    with pytest.raises(CoreAIError) as error:
        client(settings(tmp_path), handler).generate_image("海报", size="1024x1024")
    assert error.value.code == "minimax_insufficient_balance"
    assert "余额不足" in error.value.message


def test_timeout_is_reported_as_unknown_outcome(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(CoreAIError) as error:
        client(settings(tmp_path), handler).generate_image("海报", size="1024x1024")
    assert error.value.code == "gateway_timeout"
    assert error.value.status_code == 504


def test_missing_api_key_is_a_configuration_error(tmp_path):
    config = settings(tmp_path)
    config.minimax_image_api_key = ""
    with pytest.raises(CoreAIError) as error:
        client(config, _download_ok).generate_image("海报", size="1024x1024")
    assert error.value.status_code == 503


@pytest.mark.parametrize(
    "requested, expected",
    [
        ("1024x1024", "1024x1024"),
        ("1080x1920", "1080x1920"),
        ("4320x7680", "3354x5963"),
    ],
)
def test_size_is_clamped_into_the_documented_pixel_budget(requested, expected):
    assert minimax_size(requested) == expected


def test_size_keeps_a_minimum_area_for_small_edits():
    assert minimax_size("176x128") == "949x690"


def test_provider_router_keeps_gateway_models_on_core_ai(tmp_path):
    core = object()
    config = settings(tmp_path)
    assert image_provider.image_client(config, core, "gpt-image-2.5-sunburst") is core
    assert image_provider.provider_name("gpt-image-2.5-sunburst") == "core-ai"
    resolved = image_provider.image_client(config, core, "minimax-h3-image")
    assert isinstance(resolved, MiniMaxImageClient)
    assert image_provider.provider_name("minimax-h3-image") == "minimax"


def test_provider_router_refuses_unconfigured_or_unknown_minimax_models(tmp_path):
    config = settings(tmp_path)
    config.minimax_image_api_key = ""
    with pytest.raises(CoreAIError) as missing:
        image_provider.image_client(config, object(), "minimax-h3-image")
    assert missing.value.status_code == 503

    with pytest.raises(CoreAIError) as unknown:
        image_provider.image_client(settings(tmp_path), object(), "minimax-unknown")
    assert unknown.value.code == "invalid_image_model"
