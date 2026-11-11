"""Temporary direct MiniMax image provider.

core-ai does not expose MiniMax H3 Image yet, so the poster workbench can call the MiniMax
image API itself when the user picks a `minimax-*` model. This module deliberately mirrors the
tiny surface `CoreAIClient` exposes for images — `generate_image` / `edit_image` returning PNG
bytes — so `app/services/image_provider.py` can swap providers without touching callers.

Delete this module once the gateway serves the same models.
"""

from __future__ import annotations

import base64
import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from app.config import Settings
from app.services.core_ai import CoreAIError, validate_public_url
from app.services.image_payload import reference_data_url
from app.services.image_ref import ImageRef

# Workbench model ids -> MiniMax API model names.
MINIMAX_MODELS: dict[str, str] = {
    "minimax-h3-image": "MiniMax-H3-Image",
}

MODEL_PREFIX = "minimax-"

# Documented limits: 655,360 - 20,000,000 px, both edges >= 128, long/short edge <= 3:1.
MIN_PIXELS = 655_360
MAX_PIXELS = 20_000_000
MIN_SIDE = 128
MAX_SIDE_RATIO = 3
MAX_REFERENCES = 16


class MiniMaxImageClient:
    """One-shot MiniMax image calls; the API is not idempotent, so nothing is retried."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        validate_url: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self._transport = transport
        self._validate_url = validate_url or validate_public_url

    @property
    def configured(self) -> bool:
        return bool(self.settings.minimax_image_api_key.strip())

    def generate_image(
        self,
        prompt: str,
        *,
        size: str,
        model: str | None = None,
        quality: str | None = None,
    ) -> bytes:
        if not prompt.strip():
            raise CoreAIError("image_prompt_required", "生图提示词不能为空", 422)
        payload: dict[str, Any] = {
            "model": _api_model(model or self.settings.minimax_image_model),
            "prompt": prompt,
            "n": 1,
            "size": minimax_size(size),
            "response_format": "url",
            "output_format": "png",
        }
        # Off by default: our prompt already carries the fidelity rules, and a rewritten
        # prompt would drop them. The document requires an explicit size in this mode.
        if self.settings.minimax_disable_prompt_enhanced:
            payload["disable_prompt_enhanced"] = True
        result = self._post("/images/generations", payload)
        return self._image_bytes(result)

    def edit_image(
        self,
        prompt: str,
        references: list[Path | ImageRef],
        *,
        size: str,
        model: str | None = None,
        quality: str | None = None,
        mask: Path | None = None,
    ) -> bytes:
        if not prompt.strip():
            raise CoreAIError("image_prompt_required", "改图提示词不能为空", 422)
        if mask is not None:
            raise CoreAIError(
                "unsupported_mask",
                "MiniMax 暂不支持局部蒙版改图，请改用 core-ai 图片模型。",
                422,
            )
        if not references:
            raise CoreAIError("image_required", "改图至少需要一张参考图", 422)
        if len(references) > MAX_REFERENCES:
            raise CoreAIError(
                "references_too_many",
                f"MiniMax 单次最多接收 {MAX_REFERENCES} 张参考图。",
                422,
            )
        payload: dict[str, Any] = {
            "model": _api_model(model or self.settings.minimax_image_model),
            "prompt": prompt,
            "images": [{"image_url": url} for url in self._reference_urls(references)],
            "n": 1,
            "size": minimax_size(size),
            "response_format": "url",
            "output_format": "png",
        }
        result = self._post("/images/edits", payload)
        return self._image_bytes(result)

    def _reference_urls(self, references: list[Path | ImageRef]) -> list[str]:
        """Always inline the reference bytes: our own image host is not reachable by MiniMax."""
        urls: list[str] = []
        for reference in references:
            path = reference.path if isinstance(reference, ImageRef) else reference
            if path is None:
                url = reference.url if isinstance(reference, ImageRef) else None
                if not url:
                    raise CoreAIError(
                        "invalid_reference",
                        "参考图没有可用的本地路径或公网地址",
                        422,
                    )
                urls.append(url)
                continue
            urls.append(reference_data_url(path))
        return urls

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        api_key = self.settings.minimax_image_api_key.strip()
        if not api_key:
            raise CoreAIError(
                "not_configured", "未配置 MINIMAX_IMAGE_API_KEY", 503
            )
        url = f"{self.settings.minimax_image_base_url.rstrip('/')}{path}"
        timeout = float(self.settings.minimax_image_timeout_seconds)
        try:
            with self._client(timeout) as client:
                response = client.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
        except httpx.TimeoutException as exc:
            raise CoreAIError(
                "gateway_timeout",
                "MiniMax 图片请求超时，结果未知，可能已产生费用；请确认后再重新生成。",
                504,
            ) from exc
        except httpx.HTTPError as exc:
            raise CoreAIError(
                "gateway_unavailable", f"无法连接 MiniMax 图片接口：{exc}", 502
            ) from exc
        return self._json_or_error(response)

    def _client(self, timeout: float) -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(connect=15.0, read=timeout, write=180.0, pool=10.0),
            follow_redirects=True,
            transport=self._transport,
        )

    def _json_or_error(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            if response.status_code >= 400:
                raise CoreAIError(
                    "minimax_http_error",
                    f"MiniMax 请求失败（HTTP {response.status_code}）",
                    response.status_code,
                ) from exc
            raise CoreAIError(
                "invalid_response",
                f"MiniMax 返回了非 JSON 响应（HTTP {response.status_code}）",
                response.status_code,
            )
        if response.status_code >= 400:
            message = ""
            if isinstance(payload, dict):
                base = payload.get("base_resp")
                if isinstance(base, dict):
                    message = str(base.get("status_msg") or "")
                error = payload.get("error")
                if isinstance(error, dict):
                    message = str(error.get("message") or "") or message
                elif isinstance(error, str):
                    message = error
            if not message:
                message = response.text.strip()[:300] or f"HTTP {response.status_code}"
            raise CoreAIError(
                _error_code(response.status_code),
                f"MiniMax 请求失败（HTTP {response.status_code}）：{message}",
                response.status_code,
            )
        if not isinstance(payload, dict):
            raise CoreAIError("invalid_response", "MiniMax 返回内容不是 JSON 对象")
        base = payload.get("base_resp")
        if isinstance(base, dict):
            status = base.get("status_code")
            if isinstance(status, int) and status != 0:
                raise CoreAIError(
                    "minimax_error",
                    f"MiniMax 返回错误：{base.get('status_msg') or status}",
                    502,
                )
        return payload

    def _image_bytes(self, payload: dict[str, Any]) -> bytes:
        data = payload.get("data")
        item = data[0] if isinstance(data, list) and data else None
        if not isinstance(item, dict):
            raise CoreAIError("image_missing", "MiniMax 返回成功但没有图片数据")
        error = item.get("error")
        if isinstance(error, dict):
            raise CoreAIError(
                "minimax_image_failed",
                f"MiniMax 生成失败：{error.get('message') or error.get('code') or '未知原因'}",
                502,
            )
        b64 = item.get("b64_json") or item.get("b64Json")
        if isinstance(b64, str) and b64.strip():
            value = b64.strip()
            if value.startswith("data:"):
                comma = value.find(",")
                value = value[comma + 1 :] if comma >= 0 else value
            try:
                return base64.b64decode(value)
            except ValueError as exc:
                raise CoreAIError("invalid_image", "MiniMax 返回的 base64 图片无法解析") from exc
        url = item.get("url")
        if isinstance(url, str) and url.strip():
            return self._download(url.strip())
        raise CoreAIError("image_missing", "MiniMax 返回成功但没有图片 URL 或 base64")

    def _download(self, url: str) -> bytes:
        self._validate_url(url)
        limit = self.settings.max_download_bytes
        try:
            with self._client(120.0) as client:
                with client.stream("GET", url) as response:
                    if response.status_code >= 400:
                        raise CoreAIError(
                            "download_failed",
                            f"下载 MiniMax 结果失败（HTTP {response.status_code}）",
                        )
                    self._validate_url(str(response.url))
                    content_type = response.headers.get("Content-Type", "")
                    if content_type and not content_type.lower().startswith("image/"):
                        raise CoreAIError("invalid_download", "MiniMax 结果不是图片")
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > limit:
                            raise CoreAIError(
                                "download_too_large", "MiniMax 结果超过体积限制", 413
                            )
                        chunks.append(chunk)
        except CoreAIError:
            raise
        except httpx.HTTPError as exc:
            raise CoreAIError(
                "download_failed", f"下载 MiniMax 结果失败：{exc}"
            ) from exc
        data = b"".join(chunks)
        if not data:
            raise CoreAIError("empty_download", "MiniMax 结果为空")
        return _as_png(data)


def _api_model(model: str) -> str:
    return MINIMAX_MODELS.get(model, model)


def _error_code(status_code: int) -> str:
    if status_code in {401, 403}:
        return "minimax_auth"
    if status_code == 402:
        return "minimax_insufficient_balance"
    if status_code == 422:
        return "minimax_content_safety"
    if status_code == 429:
        return "minimax_rate_limited"
    if status_code >= 500:
        return "minimax_upstream"
    return "minimax_http_error"


def minimax_size(size: str) -> str:
    """Clamp an OpenAI-style `WxH` into MiniMax's documented pixel budget."""
    try:
        width_text, height_text = str(size).lower().split("x", 1)
        width, height = int(width_text), int(height_text)
    except (ValueError, AttributeError) as exc:
        raise CoreAIError("invalid_size", f"MiniMax 尺寸格式无效：{size}", 422) from exc
    if width <= 0 or height <= 0:
        raise CoreAIError("invalid_size", f"MiniMax 尺寸格式无效：{size}", 422)
    scale = 1.0
    pixels = width * height
    if pixels < MIN_PIXELS:
        scale = (MIN_PIXELS / pixels) ** 0.5
    elif pixels > MAX_PIXELS:
        scale = (MAX_PIXELS / pixels) ** 0.5
    width = max(MIN_SIDE, int(round(width * scale)))
    height = max(MIN_SIDE, int(round(height * scale)))
    longest, shortest = max(width, height), min(width, height)
    if longest > shortest * MAX_SIDE_RATIO:
        # Keep the request valid instead of failing: crop-like ratios are not used today.
        if width > height:
            width = shortest * MAX_SIDE_RATIO
        else:
            height = shortest * MAX_SIDE_RATIO
    if width * height > MAX_PIXELS:
        shrink = (MAX_PIXELS / (width * height)) ** 0.5
        width = max(MIN_SIDE, int(width * shrink))
        height = max(MIN_SIDE, int(height * shrink))
    return f"{width}x{height}"


def _as_png(data: bytes) -> bytes:
    """The pipeline stores PNG; convert when MiniMax answers with another format."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data
    try:
        with Image.open(io.BytesIO(data)) as image:
            buffer = io.BytesIO()
            image.convert("RGBA" if "A" in image.getbands() else "RGB").save(
                buffer, format="PNG"
            )
            return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001 - provider output is untrusted
        raise CoreAIError("invalid_image", "MiniMax 返回的图片无法解析") from exc
