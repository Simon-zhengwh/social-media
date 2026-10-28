from __future__ import annotations

import base64
import io
import hashlib
import ipaddress
import json
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageOps

from app.config import Settings
from app.services.image_ref import ImageRef

MAX_REFERENCE_BYTES = 1_500_000
MAX_TOTAL_REFERENCE_BYTES = 8_000_000


@dataclass(frozen=True)
class AgentArtifact:
    file_name: str
    content_type: str
    download_url: str
    title: str = ""
    description: str = ""
    size: int | None = None


@dataclass(frozen=True)
class AgentRunResult:
    run_id: str
    status: str
    output: str
    artifacts: list[AgentArtifact] = field(default_factory=list)
    token_usage: dict[str, Any] = field(default_factory=dict)
    started_at: str | None = None
    completed_at: str | None = None


@dataclass(frozen=True)
class DownloadedAgentImage:
    data: bytes
    source_url: str
    file_name: str
    content_type: str

class CoreAIError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class CoreAIClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        headers = {}
        if settings.core_ai_api_key:
            headers["Authorization"] = f"Bearer {settings.core_ai_api_key}"
        self._client = httpx.Client(
            timeout=httpx.Timeout(
                connect=15.0,
                read=settings.core_ai_image_timeout_seconds,
                write=60.0,
                pool=10.0,
            ),
            headers=headers,
            transport=transport,
        )
        self._upload_client = httpx.Client(
            timeout=httpx.Timeout(connect=15.0, read=60.0, write=180.0, pool=10.0),
            follow_redirects=True,
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
        self._upload_client.close()

    def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: int | float | None = None,
    ) -> dict[str, Any]:
        if not self.settings.core_ai_api_key:
            raise CoreAIError("not_configured", "未配置 CORE_AI_API_KEY", 503)
        url = f"{self.settings.core_ai_gateway_base_url.rstrip('/')}{path}"
        timeout = timeout_seconds or self.settings.core_ai_image_timeout_seconds
        response: httpx.Response | None = None
        for attempt in range(2):
            try:
                response = self._client.post(url, json=payload, timeout=timeout)
            except httpx.TimeoutException as exc:
                raise CoreAIError(
                    "gateway_timeout",
                    "core-ai 网关请求超时，结果未知，可能已产生费用；请确认后再重新生成。",
                    504,
                ) from exc
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                raise CoreAIError(
                    "gateway_unavailable",
                    "无法连接 core-ai 网关，请检查网络和 CORE_AI_GATEWAY_BASE_URL。",
                    502,
                ) from exc
            except httpx.HTTPError as exc:
                raise CoreAIError(
                    "gateway_error",
                    f"core-ai 网关请求失败：{exc}",
                    502,
                ) from exc
            if response.status_code != 429 or attempt == 1:
                break
            try:
                delay = float(response.headers.get("Retry-After", "5"))
            except ValueError:
                delay = 5.0
            time.sleep(min(max(delay, 1.0), 30.0))
        assert response is not None
        return self._json_or_error(response)

    def _json_or_error(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            if response.status_code >= 400:
                message = response.text.strip()[:500] or f"HTTP {response.status_code}"
                raise CoreAIError(
                    "core_ai_http_error",
                    f"core-ai 请求失败（HTTP {response.status_code}）：{message}",
                    response.status_code,
                ) from exc
            raise CoreAIError(
                "invalid_response",
                f"core-ai 返回了非 JSON 响应（HTTP {response.status_code}）",
            ) from exc
        if response.status_code >= 400:
            message = ""
            code = ""
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict):
                    message = str(error.get("message") or "")
                    code = str(error.get("code") or "")
                elif isinstance(error, str):
                    message = error
                message = str(payload.get("message") or message)
                code = str(payload.get("errorCode") or code)
            if not message:
                message = response.text.strip()[:500] or f"HTTP {response.status_code}"
            if response.status_code in {401, 403}:
                code = code or "core_ai_auth"
            elif response.status_code == 429:
                code = code or "core_ai_rate_limited"
            elif response.status_code >= 500:
                code = code or "core_ai_upstream"
            raise CoreAIError(
                code or "core_ai_http_error",
                f"core-ai 请求失败（HTTP {response.status_code}）：{message}",
                response.status_code,
            )
        if not isinstance(payload, dict):
            raise CoreAIError("invalid_response", "core-ai 返回内容不是 JSON 对象")
        return payload

    def run_agent(self, agent_id: str, input_text: str, *, timeout_seconds: int) -> str:
        """Trigger a published Core AI agent and wait for its text result."""
        result = self.run_agent_detail(agent_id, input_text, timeout_seconds=timeout_seconds)
        output = result.output.strip()
        if output:
            return output
        raise CoreAIError("prompt_optimizer_empty", "Agent 没有返回文本结果")

    def run_agent_detail(
        self,
        agent_id: str,
        input_text: str,
        *,
        timeout_seconds: int,
        poll_interval: float = 2.0,
        on_run_id: Callable[[str], None] | None = None,
    ) -> AgentRunResult:
        """Trigger a published Core AI agent and return its full terminal run detail."""
        run_id = self.trigger_agent(
            agent_id,
            input_text,
            timeout_seconds=timeout_seconds,
        )
        if on_run_id is not None:
            on_run_id(run_id)
        return self.wait_agent_detail(
            run_id,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        )

    def trigger_agent(
        self,
        agent_id: str,
        input_text: str,
        *,
        timeout_seconds: int,
    ) -> str:
        """Start an agent run and return its run_id without waiting for completion."""
        if not self.settings.core_ai_api_key:
            raise CoreAIError("not_configured", "未配置 CORE_AI_API_KEY", 503)
        if not agent_id.strip():
            raise CoreAIError("invalid_agent", "Agent ID 不能为空", 422)
        if timeout_seconds <= 0:
            raise CoreAIError("invalid_timeout", "Agent 超时时间必须大于 0", 422)
        base = self.settings.core_ai_server_base_url.rstrip("/")
        try:
            response = self._client.post(
                f"{base}/api/runs/agent/{agent_id}/trigger",
                json={"input": input_text},
                timeout=min(timeout_seconds, 30),
            )
            run = self._json_or_error(response)
            candidate = run.get("run_id")
            if not isinstance(candidate, str) or not candidate:
                raise CoreAIError("invalid_agent_run", "Agent 未返回有效 run_id")
            return candidate
        except httpx.TimeoutException as exc:
            raise CoreAIError("agent_timeout", "图片 Agent 请求超时，结果未知", 504) from exc
        except httpx.HTTPError as exc:
            raise CoreAIError("agent_unavailable", f"无法连接图片 Agent：{exc}") from exc

    def wait_agent_detail(
        self,
        run_id: str,
        *,
        timeout_seconds: int,
        poll_interval: float = 2.0,
    ) -> AgentRunResult:
        """Wait for an existing agent run so a service restart can resume polling."""
        if not self.settings.core_ai_api_key:
            raise CoreAIError("not_configured", "未配置 CORE_AI_API_KEY", 503)
        if not run_id.strip():
            raise CoreAIError("invalid_agent_run", "Agent run_id 不能为空", 422)
        if timeout_seconds <= 0:
            raise CoreAIError("invalid_timeout", "Agent 超时时间必须大于 0", 422)
        base = self.settings.core_ai_server_base_url.rstrip("/")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                response = self._client.get(f"{base}/api/runs/{run_id}", timeout=30)
                detail = self._json_or_error(response)
            except (httpx.TimeoutException, httpx.NetworkError):
                if time.monotonic() >= deadline:
                    break
                time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
                continue
            status = str(detail.get("status") or "")
            if status == "COMPLETED":
                output = detail.get("output")
                return AgentRunResult(
                    run_id=run_id,
                    status=status,
                    output=output if isinstance(output, str) else "",
                    artifacts=self._agent_artifacts(detail.get("artifacts")),
                    token_usage=detail.get("token_usage") if isinstance(detail.get("token_usage"), dict) else {},
                    started_at=detail.get("started_at") if isinstance(detail.get("started_at"), str) else None,
                    completed_at=detail.get("completed_at") if isinstance(detail.get("completed_at"), str) else None,
                )
            if status in {"FAILED", "TIMEOUT", "CANCELLED", "SKIPPED"}:
                message = str(detail.get("error") or "Agent 运行失败")
                raise CoreAIError("agent_run_failed", f"图片 Agent 运行失败：{message}")
            if status not in {"PENDING", "RUNNING"}:
                raise CoreAIError("invalid_agent_run", "Agent 返回了未知运行状态")
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
        raise CoreAIError("agent_timeout", f"图片 Agent 等待超时（run_id: {run_id}）", 504)

    def _agent_artifacts(self, raw: Any) -> list[AgentArtifact]:
        artifacts: list[AgentArtifact] = []
        if not isinstance(raw, list):
            return artifacts
        for item in raw:
            if not isinstance(item, dict):
                continue
            url = item.get("download_url")
            content_type = str(item.get("content_type") or "")
            if not isinstance(url, str) or not url.startswith("https://"):
                continue
            if content_type and not content_type.startswith("image/"):
                continue
            artifacts.append(
                AgentArtifact(
                    file_name=str(item.get("file_name") or "agent-image.png"),
                    content_type=content_type or "image/png",
                    download_url=url,
                    title=str(item.get("title") or ""),
                    description=str(item.get("description") or ""),
                    size=int(item["size"]) if isinstance(item.get("size"), int) else None,
                )
            )
        return artifacts

    def download_agent_images(self, result: AgentRunResult) -> list[DownloadedAgentImage]:
        urls: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for artifact in result.artifacts:
            if artifact.download_url in seen:
                continue
            seen.add(artifact.download_url)
            urls.append((artifact.download_url, artifact.file_name, artifact.content_type))
        if not urls:
            for index, url in enumerate(self._markdown_image_links(result.output), start=1):
                if url in seen:
                    continue
                seen.add(url)
                urls.append((url, f"agent-image-{index}.png", "image/png"))
        if not urls:
            raise CoreAIError("agent_image_missing", "Agent 已完成，但没有返回图片")
        images: list[DownloadedAgentImage] = []
        seen_digests: set[str] = set()
        last_error: CoreAIError | None = None
        for url, file_name, content_type in urls:
            try:
                data = self.download_image(url)
                digest = hashlib.sha256(data).hexdigest()
                if digest in seen_digests:
                    continue
                seen_digests.add(digest)
                images.append(
                    DownloadedAgentImage(
                        data=data,
                        source_url=url,
                        file_name=file_name,
                        content_type=content_type,
                    )
                )
            except CoreAIError as exc:
                last_error = exc
        if not images:
            raise last_error or CoreAIError("download_failed", "Agent 图片下载失败")
        return images

    def _markdown_image_links(self, output: str) -> list[str]:
        links = re.findall(r"!\[[^\]]*\]\(<?([^\s)>]+)>?(?:\s+\"[^\"]*\")?\)", output)
        return list(dict.fromkeys(link for link in links if link.startswith("https://")))

    def upload_image_blob(self, data: bytes, *, content_type: str = "image/png") -> str:
        """Upload an image through Core AI's public blob credential flow and return its URL."""
        if not self.settings.core_ai_api_key:
            raise CoreAIError("not_configured", "未配置 CORE_AI_API_KEY", 503)
        if not data:
            raise CoreAIError("empty_upload", "待上传图片为空", 422)
        base = self.settings.core_ai_server_base_url.rstrip("/")
        response = self._client.get(
            f"{base}/api/blob/upload-credential",
            params={"content_type": content_type, "category": "multimodal"},
            timeout=30,
        )
        credential = self._json_or_error(response)
        upload_url = credential.get("upload_url")
        blob_url = credential.get("blob_url")
        if not isinstance(upload_url, str) or not upload_url.startswith("https://"):
            raise CoreAIError("invalid_upload_credential", "Core AI 未返回有效的图片上传地址")
        if not isinstance(blob_url, str) or not blob_url.startswith("https://"):
            raise CoreAIError("invalid_upload_credential", "Core AI 未返回有效的公网图片地址")
        try:
            upload = self._upload_client.put(
                upload_url,
                content=data,
                headers={
                    "Content-Type": content_type,
                    "x-ms-blob-type": "BlockBlob",
                    "x-ms-blob-content-type": content_type,
                },
                timeout=180,
            )
        except httpx.HTTPError as exc:
            raise CoreAIError("image_upload_failed", f"图片上传 Core AI 失败：{exc}") from exc
        if upload.status_code >= 400:
            raise CoreAIError(
                "image_upload_failed",
                f"图片上传 Core AI 失败（HTTP {upload.status_code}）",
            )
        self._validate_public_url(blob_url)
        return blob_url

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
        payload = {
            "model": model or self.settings.core_ai_image_model,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "quality": quality or self.settings.core_ai_image_quality,
            "output_format": "png",
        }
        result = self._post_json("/images/generations", payload)
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
        if not references and mask is None:
            raise CoreAIError("image_required", "改图至少需要一张输入图片或蒙版", 422)
        payload: dict[str, Any] = {
            "model": model or self.settings.core_ai_image_model,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "quality": quality or self.settings.core_ai_image_quality,
            "output_format": "png",
            "input_images": self._references_payload(references),
        }
        if mask is not None:
            payload["mask"] = {"b64Json": self._reference_data_url(mask, keep_alpha=True)}
        result = self._post_json("/images/edits", payload)
        return self._image_bytes(result)

    def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        payload = {
            "model": model or self.settings.core_ai_design_model,
            "messages": messages,
            "stream": False,
        }
        data = self._post_json(
            "/chat/completions",
            payload,
            timeout_seconds=timeout_seconds or self.settings.llm_timeout_seconds,
        )
        choices = data.get("choices")
        message = choices[0].get("message") if isinstance(choices, list) and choices else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict)
            )
        if not isinstance(content, str) or not content.strip():
            raise CoreAIError("design_empty", "设计模型没有返回内容")
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.lower().startswith("json"):
                text = text[4:].lstrip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise CoreAIError("design_invalid_json", "设计模型没有返回 JSON 对象")
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise CoreAIError("design_invalid_json", "设计模型返回的 JSON 无法解析") from exc
        if not isinstance(parsed, dict):
            raise CoreAIError("design_invalid_json", "设计模型返回内容不是对象")
        return parsed

    def _references_payload(self, references: list[Path | ImageRef]) -> list[dict[str, str]]:
        payload: list[dict[str, str]] = []
        total = 0
        for reference in references:
            if isinstance(reference, ImageRef):
                if reference.url:
                    payload.append({"url": reference.url})
                    continue
                if reference.path is None:
                    raise CoreAIError(
                        "invalid_reference",
                        "参考图没有可用的本地路径或公网地址",
                        422,
                    )
                path = reference.path
            else:
                path = reference
            # Transparency matters for logos; opaque product photos stay JPEG.
            data_url = self._reference_data_url(path)
            total += len(data_url.encode("utf-8"))
            if total > MAX_TOTAL_REFERENCE_BYTES:
                raise CoreAIError(
                    "references_too_large",
                    "参考图总大小超过网关限制，请减少商品图数量或尺寸。",
                    413,
                )
            payload.append({"b64Json": data_url})
        return payload

    def _reference_data_url(self, path: Path, *, keep_alpha: bool | None = None) -> str:
        try:
            with Image.open(path) as source:
                image = ImageOps.exif_transpose(source)
                image.load()
                has_alpha = (
                    "A" in image.getbands()
                    and image.convert("RGBA").getchannel("A").getextrema()[0] < 250
                )
                if keep_alpha is None:
                    keep_alpha = has_alpha
                image = image.convert("RGBA" if keep_alpha else "RGB")
        except Exception as exc:  # noqa: BLE001 - provider input is untrusted
            raise CoreAIError("invalid_reference", f"参考图无法读取：{path.name}", 422) from exc

        if keep_alpha:
            data, mime = self._encode_png(image), "image/png"
        else:
            data, mime = self._encode_jpeg(image), "image/jpeg"
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    def _encode_jpeg(self, image: Image.Image) -> bytes:
        current = image.copy()
        current.thumbnail((1536, 1536), Image.Resampling.LANCZOS)
        for _ in range(6):
            for quality in (88, 82, 76, 70, 64):
                buffer = io.BytesIO()
                current.save(buffer, format="JPEG", quality=quality, optimize=True)
                data = buffer.getvalue()
                if len(data) <= MAX_REFERENCE_BYTES:
                    return data
            current.thumbnail(
                (max(256, int(current.width * 0.85)), max(256, int(current.height * 0.85))),
                Image.Resampling.LANCZOS,
            )
        buffer = io.BytesIO()
        current.save(buffer, format="JPEG", quality=60, optimize=True)
        return buffer.getvalue()

    def _encode_png(self, image: Image.Image) -> bytes:
        current = image.copy()
        current.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        for _ in range(6):
            buffer = io.BytesIO()
            current.save(buffer, format="PNG", optimize=True)
            data = buffer.getvalue()
            if len(data) <= MAX_REFERENCE_BYTES:
                return data
            current.thumbnail(
                (max(128, int(current.width * 0.8)), max(128, int(current.height * 0.8))),
                Image.Resampling.LANCZOS,
            )
        return data

    def _image_bytes(self, payload: dict[str, Any]) -> bytes:
        data = payload.get("data")
        item = data[0] if isinstance(data, list) and data else None
        if not isinstance(item, dict):
            raise CoreAIError("image_missing", "core-ai 返回成功但没有图片数据")
        b64_json = item.get("b64_json") or item.get("b64Json")
        if isinstance(b64_json, str) and b64_json.strip():
            value = b64_json.strip()
            if value.startswith("data:"):
                comma = value.find(",")
                value = value[comma + 1 :] if comma >= 0 else value
            try:
                return base64.b64decode(value)
            except ValueError as exc:
                raise CoreAIError("invalid_image", "core-ai 返回的 base64 图片无法解析") from exc
        url = item.get("url")
        if isinstance(url, str) and url.strip():
            return self.download_image(url.strip())
        raise CoreAIError("image_missing", "core-ai 返回成功但没有图片 URL 或 base64")

    def download_image(self, url: str) -> bytes:
        self._validate_public_url(url)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with self._download_client.stream("GET", url) as response:
                    if response.status_code >= 400:
                        raise CoreAIError(
                            "download_failed",
                            f"下载生图结果失败（HTTP {response.status_code}）",
                        )
                    self._validate_public_url(str(response.url))
                    content_type = response.headers.get("Content-Type", "")
                    if content_type and not content_type.lower().startswith("image/"):
                        raise CoreAIError("invalid_download", "生图结果不是图片")
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > self.settings.max_download_bytes:
                            raise CoreAIError("download_too_large", "生图结果超过体积限制")
                        chunks.append(chunk)
                data = b"".join(chunks)
                if not data:
                    raise CoreAIError("empty_download", "生图结果为空")
                return data
            except CoreAIError:
                raise
            except (httpx.HTTPError, OSError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1.5 + attempt)
        raise CoreAIError(
            "download_failed",
            f"生图结果下载失败：{last_error}",
        )

    def _validate_public_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise CoreAIError("unsafe_download", "生图结果地址不是合法的 HTTPS URL")
        if parsed.username or parsed.password:
            raise CoreAIError("unsafe_download", "生图结果地址不能包含认证信息")
        try:
            addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise CoreAIError(
                "download_failed",
                f"无法解析生图结果地址：{parsed.hostname}",
            ) from exc
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0].split("%", 1)[0])
            candidates = [ip]
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
                candidates.append(ip.ipv4_mapped)
            if any(
                value.is_private
                or value.is_loopback
                or value.is_link_local
                or value.is_multicast
                or value.is_reserved
                or value.is_unspecified
                for value in candidates
            ):
                raise CoreAIError("unsafe_download", "生图结果地址指向内网，已拒绝下载")
