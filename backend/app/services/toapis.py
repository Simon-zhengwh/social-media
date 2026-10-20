from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from app.config import Settings

WAITING_STATUSES = {"pending", "queued", "in_progress", "processing"}
ALLOWED_DOWNLOAD_SUFFIXES = (".toapis.xyz", ".toapis.com")


class ToAPIsError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class ToAPIsClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._client = httpx.Client(
            timeout=httpx.Timeout(connect=30.0, read=120.0, write=60.0, pool=10.0),
            headers={"Authorization": f"Bearer {settings.toapis_api_key}"},
            transport=transport,
        )
        self._download_client = httpx.Client(
            timeout=httpx.Timeout(connect=15.0, read=120.0, write=15.0, pool=10.0),
            follow_redirects=True,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()
        self._download_client.close()

    def _json_or_error(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ToAPIsError(
                "invalid_response",
                f"ToAPIs 返回了非 JSON 响应（HTTP {response.status_code}）",
            ) from exc
        if response.status_code >= 400:
            error = payload.get("error") if isinstance(payload, dict) else None
            message = ""
            code = ""
            if isinstance(error, dict):
                message = str(error.get("message") or "")
                code = str(error.get("code") or "")
            raise ToAPIsError(
                code or "toapis_http_error",
                message or f"ToAPIs 请求失败（HTTP {response.status_code}）",
                status_code=response.status_code,
            )
        return payload

    def upload_reference(self, path: Path) -> str:
        mime = "image/png"
        if path.suffix.lower() in {".jpg", ".jpeg"}:
            mime = "image/jpeg"
        elif path.suffix.lower() == ".webp":
            mime = "image/webp"
        files = {"file": (path.name, path.read_bytes(), mime)}
        for attempt in range(2):
            try:
                response = self._client.post(
                    f"{self.settings.toapis_upload_base_url.rstrip('/')}/v1/uploads/images",
                    files=files,
                    data={"purpose": "generation"},
                )
                break
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if attempt == 1:
                    raise
                time.sleep(1.0)
        payload = self._json_or_error(response)
        data = payload.get("data") if isinstance(payload, dict) else None
        url = data.get("url") if isinstance(data, dict) else None
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ToAPIsError("upload_missing_url", "参考图上传成功但没有返回可用 URL")
        return url

    def submit_generation(
        self,
        prompt: str,
        reference_urls: list[str],
        ratio: str | None = None,
        resolution: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "model": self.settings.toapis_image_model,
            "prompt": prompt,
            "quality": "high",
            "size": ratio or self.settings.image_ratio,
            "resolution": resolution or self.settings.image_resolution,
            "n": 1,
            "reference_images": reference_urls,
        }
        response = self._client.post(
            f"{self.settings.toapis_image_base_url.rstrip('/')}/v1/images/generations",
            json=payload,
        )
        result = self._json_or_error(response)
        task_id = result.get("id") if isinstance(result, dict) else None
        if not isinstance(task_id, str) or not task_id:
            raise ToAPIsError("missing_task_id", "生图提交成功但没有返回任务 ID")
        return result

    def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Call the multimodal chat endpoint and parse a JSON object response."""
        payload = {
            "model": model or self.settings.toapis_design_model,
            "messages": messages,
            "stream": False,
        }
        response = self._client.post(
            f"{self.settings.toapis_chat_base_url.rstrip('/')}/v1/chat/completions",
            json=payload,
            timeout=timeout_seconds or self.settings.llm_timeout_seconds,
        )
        data = self._json_or_error(response)
        choices = data.get("choices") if isinstance(data, dict) else None
        message = choices[0].get("message") if isinstance(choices, list) and choices else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict)
            )
        if not isinstance(content, str) or not content.strip():
            raise ToAPIsError("design_empty", "设计模型没有返回内容")
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.lower().startswith("json"):
                text = text[4:].lstrip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ToAPIsError("design_invalid_json", "设计模型没有返回 JSON 对象")
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ToAPIsError("design_invalid_json", "设计模型返回的 JSON 无法解析") from exc
        if not isinstance(parsed, dict):
            raise ToAPIsError("design_invalid_json", "设计模型返回内容不是对象")
        return parsed

    def get_generation(self, task_id: str) -> dict[str, Any]:
        response = self._client.get(
            f"{self.settings.toapis_image_base_url.rstrip('/')}/v1/images/generations/{task_id}"
        )
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "5")
            try:
                delay = max(1.0, float(retry_after))
            except ValueError:
                delay = 5.0
            time.sleep(min(delay, 30.0))
            response = self._client.get(
                f"{self.settings.toapis_image_base_url.rstrip('/')}/v1/images/generations/{task_id}"
            )
        return self._json_or_error(response)

    def wait_for_generation(
        self,
        task_id: str,
        on_update: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        deadline = time.monotonic() + self.settings.image_wait_timeout_seconds
        last_payload: dict[str, Any] = {}
        while True:
            last_payload = self.get_generation(task_id)
            if on_update is not None:
                on_update(last_payload)
            status = str(last_payload.get("status") or "").lower()
            if status == "completed":
                result = last_payload.get("result")
                data = result.get("data") if isinstance(result, dict) else None
                url = data[0].get("url") if isinstance(data, list) and data else None
                if not isinstance(url, str) or not url.startswith("https://"):
                    raise ToAPIsError("completed_without_url", "生图完成但没有返回图片 URL")
                return url
            if status == "failed":
                error = last_payload.get("error")
                message = "生图任务失败"
                code = "generation_failed"
                if isinstance(error, dict):
                    message = str(error.get("message") or message)
                    code = str(error.get("code") or code)
                raise ToAPIsError(code, message)
            if status not in WAITING_STATUSES:
                raise ToAPIsError(
                    "unknown_status",
                    f"未知的生图任务状态：{status or '空'}",
                )
            if time.monotonic() >= deadline:
                raise ToAPIsError(
                    "generation_timeout",
                    "生图等待超时，任务仍可能在供应商侧处理中",
                    status_code=504,
                )
            time.sleep(self.settings.image_poll_interval_seconds + random.uniform(0.0, 1.0))

    def download_result(self, url: str) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise ToAPIsError("unsafe_download", "生图结果地址不是 HTTPS")
        if not parsed.hostname or not parsed.hostname.endswith(ALLOWED_DOWNLOAD_SUFFIXES):
            raise ToAPIsError("unsafe_download", "生图结果地址不在允许的文件域名内")

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return self._download_once(url)
            except ToAPIsError:
                raise
            except (httpx.HTTPError, OSError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1.5 + attempt)
        raise ToAPIsError(
            "download_failed",
            "生图结果下载中断，已重试 3 次仍失败，请重新发送修改。",
        )

    def _download_once(self, url: str) -> bytes:
        with self._download_client.stream("GET", url) as response:
            if response.status_code >= 400:
                raise ToAPIsError(
                    "download_failed",
                    f"下载生图结果失败（HTTP {response.status_code}）",
                )
            final_host = response.url.host or ""
            if not final_host.endswith(ALLOWED_DOWNLOAD_SUFFIXES):
                raise ToAPIsError("unsafe_download", "生图结果重定向到了非允许域名")
            content_type = response.headers.get("Content-Type", "")
            if content_type and not content_type.lower().startswith("image/"):
                raise ToAPIsError("invalid_download", "生图结果不是图片")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > self.settings.max_download_bytes:
                    raise ToAPIsError("download_too_large", "生图结果超过体积限制")
                chunks.append(chunk)
        data = b"".join(chunks)
        if not data:
            raise ToAPIsError("empty_download", "生图结果为空")
        return data
