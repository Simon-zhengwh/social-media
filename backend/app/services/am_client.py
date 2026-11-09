from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


class AMClientError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AMUploadTicket:
    file_key: str
    upload_url: str
    expires_at: str | None


class AMClient:
    """Small server-side AM Console client. Signed URLs are never logged."""

    def __init__(self, settings: Settings, *, http_client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.base_url = settings.am_base_url.strip().rstrip("/")
        self.http = http_client or httpx.Client(timeout=settings.am_timeout_seconds, follow_redirects=True)

    def close(self) -> None:
        self.http.close()

    def access(self, cookie: str, merchant_id: str) -> dict[str, Any]:
        return self._get(
            f"/ajax/merchant/{quote(merchant_id, safe='')}/access",
            cookie=cookie,
        )

    def profile(self, cookie: str, merchant_id: str) -> dict[str, Any] | None:
        try:
            return self._get(
                f"/ajax/merchant/{quote(merchant_id, safe='')}/profile",
                cookie=cookie,
            )
        except AMClientError as exc:
            if exc.status_code == 404:
                return None
            raise

    def search_merchants(
        self,
        cookie: str,
        *,
        keyword: str = "",
        skip: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """The merchants AM Console offers on its own image-tool launcher.

        Same endpoint the console page uses, so the switcher cannot drift from it. The list
        is deliberately unfiltered by primary AM or status: the UAT console opens every
        merchant that has a restaurant brand profile.
        """
        return self._request(
            "PUT",
            "/ajax/image-tools/merchant/search",
            cookie=cookie,
            json={
                "keyword": keyword.strip() or None,
                "skip": skip,
                "limit": max(1, min(limit, 100)),
            },
        )

    def search_media(
        self,
        cookie: str,
        merchant_id: str,
        *,
        keyword: str = "",
        skip: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        return self._request(
            "PUT",
            "/ajax/media-library/search",
            cookie=cookie,
            json={
                "merchant_id": merchant_id,
                "filename": keyword.strip() or None,
                "skip": skip,
                "limit": max(1, min(limit, 100)),
            },
        )

    def get_media(self, cookie: str, merchant_id: str, media_library_id: str) -> dict[str, Any]:
        payload = self._get(
            f"/ajax/media-library/get/{quote(media_library_id, safe='')}",
            cookie=cookie,
            params={"merchant_id": merchant_id},
        )
        media = payload.get("media_library") if isinstance(payload, dict) else None
        if not isinstance(media, dict):
            raise AMClientError(502, "am_invalid_response", "AM 素材详情响应格式无效")
        return media

    def initiate_media_upload(
        self,
        *,
        merchant_id: str,
        kind: str,
        filename: str,
    ) -> AMUploadTicket:
        payload = self._request(
            "POST",
            "/internal/media/files/init-upload",
            service_auth=True,
            json={"merchant_id": merchant_id, "kind": kind, "filename": filename},
        )
        try:
            return AMUploadTicket(
                file_key=str(payload["file_key"]),
                upload_url=str(payload["upload_url"]),
                expires_at=str(payload.get("expires_at") or "") or None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AMClientError(502, "am_invalid_response", "AM 上传签发响应格式无效") from exc

    def complete_media_upload(
        self,
        *,
        merchant_id: str,
        kind: str,
        file_key: str,
    ) -> dict[str, Any]:
        payload = self._request(
            "PUT",
            "/internal/media/files/complete-upload",
            service_auth=True,
            json={"merchant_id": merchant_id, "kind": kind, "file_key": file_key},
        )
        if not isinstance(payload, dict) or not payload.get("url") or not payload.get("size"):
            raise AMClientError(502, "am_invalid_response", "AM 上传确认响应格式无效")
        return payload

    def _get(
        self,
        path: str,
        *,
        cookie: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request("GET", path, cookie=cookie, params=params)

    def _request(
        self,
        method: str,
        path: str,
        *,
        cookie: str | None = None,
        service_auth: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        if service_auth:
            key = self.settings.am_media_storage_service_key.strip()
            if not key:
                raise AMClientError(503, "media_storage_not_configured", "未配置 AM 媒体存储服务身份")
            headers["Authorization"] = f"Bearer {key}"
        try:
            response = self.http.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
        except httpx.RequestError as exc:
            raise AMClientError(502, "am_unavailable", "无法连接 AM Console，请稍后重试") from exc
        if response.status_code >= 400:
            code, message = self._error(response)
            raise AMClientError(response.status_code, code, message)
        try:
            payload = response.json()
        except ValueError as exc:
            raise AMClientError(502, "am_invalid_response", "AM Console 没有返回有效 JSON") from exc
        if not isinstance(payload, dict):
            raise AMClientError(502, "am_invalid_response", "AM Console 响应格式无效")
        return payload

    @staticmethod
    def _error(response: httpx.Response) -> tuple[str, str]:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        code = f"am_http_{response.status_code}"
        message = f"AM Console 请求失败（HTTP {response.status_code}）"
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                code = str(error.get("code") or code)
                message = str(error.get("message") or message)
            elif payload.get("error_code"):
                code = str(payload["error_code"])
                message = str(payload.get("message") or message)
        return code, message
