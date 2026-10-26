from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx


class ImageHostError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ImageHostUpload:
    url: str
    path: str
    sha256: str


def _public_url(base_url: str, uploaded_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if not base:
        return uploaded_url
    parsed = urlsplit(uploaded_url)
    if not parsed.path.startswith("/"):
        raise ImageHostError("image_host_invalid_response", "图床返回的图片地址无效")
    return f"{base}{parsed.path}"


class ImageHostClient:
    def __init__(self, settings: Any, *, http_client: httpx.Client | None = None) -> None:
        base_url = settings.image_host_base_url.strip().rstrip("/")
        token = settings.image_host_upload_token.strip()
        if not base_url or not token:
            raise ImageHostError("image_host_not_configured", "未配置图床地址或上传令牌")
        self.settings = settings
        self.base_url = base_url
        self.token = token
        self.http = http_client or httpx.Client(timeout=settings.image_host_timeout_seconds)

    def close(self) -> None:
        self.http.close()

    def upload(self, filename: str, data: bytes) -> ImageHostUpload:
        if not data:
            raise ImageHostError("image_host_empty_file", "不能上传空图片")
        encoded_name = quote(Path(filename).name or "image.png", safe="")
        try:
            response = self.http.post(
                f"{self.base_url}/api/upload",
                content=data,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "X-Filename": encoded_name,
                    "Content-Type": "application/octet-stream",
                },
            )
        except httpx.RequestError as exc:
            raise ImageHostError("image_host_unavailable", "无法连接图床，请稍后重试") from exc

        if response.status_code == 401:
            raise ImageHostError("image_host_auth", "图床上传令牌无效")
        if response.status_code == 413:
            raise ImageHostError("image_host_file_too_large", "图片超过图床 25 MB 限制")
        if response.status_code == 415:
            raise ImageHostError("image_host_unsupported", "图床不支持该图片格式")
        if response.status_code == 507:
            raise ImageHostError("image_host_quota", "图床容量已满，请先删除旧图")
        if response.status_code != 200:
            raise ImageHostError("image_host_upload_failed", f"图床上传失败（HTTP {response.status_code}）")

        try:
            payload = response.json()
        except ValueError as exc:
            raise ImageHostError("image_host_invalid_response", "图床没有返回有效 JSON") from exc
        uploaded_url = payload.get("url") if isinstance(payload, dict) else None
        path = payload.get("path") if isinstance(payload, dict) else None
        sha256 = payload.get("sha256") if isinstance(payload, dict) else None
        if not isinstance(uploaded_url, str) or not isinstance(path, str):
            raise ImageHostError("image_host_invalid_response", "图床返回缺少图片地址")
        return ImageHostUpload(
            url=_public_url(self.settings.image_host_public_base_url, uploaded_url),
            path=path,
            sha256=str(sha256 or ""),
        )


def upload_asset(
    db: Any,
    store: Any,
    settings: Any,
    asset_id: str,
    *,
    client: Any | None = None,
) -> str:
    asset = db.assets.find_one({"_id": asset_id})
    if asset is None:
        raise ImageHostError("reference_unavailable", f"参考图不存在：{asset_id}")
    cached = str(asset.get("image_host_url") or "").strip()
    if cached:
        return cached
    key = str(asset.get("prepared_path") or asset.get("path") or "")
    if not key:
        raise ImageHostError("reference_unavailable", f"参考图没有本地文件：{asset_id}")
    try:
        data = store.get_bytes(key)
    except Exception as exc:  # noqa: BLE001 - local object store errors vary
        raise ImageHostError("reference_unavailable", f"无法读取参考图：{asset_id}") from exc

    host = client or ImageHostClient(settings)
    try:
        uploaded = host.upload(Path(key).name, data)
    finally:
        if client is None:
            host.close()
    db.assets.update_one(
        {"_id": asset_id},
        {
            "$set": {
                "image_host_url": uploaded.url,
                "image_host_path": uploaded.path,
                "image_host_sha256": uploaded.sha256 or None,
                "image_host_uploaded_at": datetime.now(timezone.utc),
            }
        },
    )
    return uploaded.url
