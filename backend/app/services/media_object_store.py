from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.services.am_client import AMClient, AMClientError
from app.services.object_store import ObjectStoreError

logger = logging.getLogger(__name__)


class MediaObjectStore:
    """ObjectStore backed by AM-issued Azure SAS uploads and a merchant-scoped mapping."""

    def __init__(self, settings: Settings, db: Any, am: AMClient, merchant_id: str) -> None:
        self.settings = settings
        self.db = db
        self.am = am
        self.merchant_id = merchant_id
        self.http = httpx.Client(timeout=settings.am_timeout_seconds, follow_redirects=True)

    def close(self) -> None:
        self.http.close()

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        kind = self._kind(key)
        filename = Path(key).name
        now = datetime.now(timezone.utc)
        existing = self.db.media_objects.find_one({"_id": key})
        if existing and existing.get("status") == "ready":
            raise ObjectStoreError("对象键已存在，拒绝覆盖不可变文件")
        self.db.media_objects.replace_one(
            {"_id": key},
            {
                "_id": key,
                "logical_key": key,
                "kind": kind,
                "status": "pending",
                "file_key": None,
                "url": None,
                "size": None,
                "content_type": content_type,
                "created_at": (existing or {}).get("created_at") or now,
                "updated_at": now,
            },
            upsert=True,
        )
        try:
            ticket = self.am.initiate_media_upload(
                merchant_id=self.merchant_id,
                kind=kind,
                filename=filename,
            )
            self.db.media_objects.update_one(
                {"_id": key},
                {"$set": {"file_key": ticket.file_key, "status": "uploading", "updated_at": datetime.now(timezone.utc)}},
            )
            try:
                response = self.am.http.put(
                    ticket.upload_url,
                    content=data,
                    headers={
                        "Content-Type": content_type,
                        "x-ms-blob-type": "BlockBlob",
                    },
                )
            except httpx.RequestError as exc:
                raise ObjectStoreError("Azure 上传连接失败") from exc
            if response.status_code not in {200, 201}:
                raise ObjectStoreError(f"Azure 上传失败（HTTP {response.status_code}）")
            completed = self.am.complete_media_upload(
                merchant_id=self.merchant_id,
                kind=kind,
                file_key=ticket.file_key,
            )
            self.db.media_objects.update_one(
                {"_id": key},
                {
                    "$set": {
                        "status": "ready",
                        "url": str(completed["url"]),
                        "size": int(completed["size"]),
                        "confirmed_at": datetime.now(timezone.utc),
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
            )
        except (AMClientError, ObjectStoreError, OSError, ValueError):
            self.db.media_objects.update_one(
                {"_id": key},
                {"$set": {"status": "orphaned", "updated_at": datetime.now(timezone.utc)}},
            )
            raise

    def get_bytes(self, key: str) -> bytes:
        mapping = self._ready_mapping(key)
        try:
            response = self.am.http.get(str(mapping["url"]))
        except httpx.RequestError as exc:
            raise ObjectStoreError("读取 Azure 媒体失败") from exc
        if response.status_code != 200:
            raise ObjectStoreError(f"读取 Azure 媒体失败（HTTP {response.status_code}）")
        limit = (self.settings.max_agent_artifact_bytes if "artifacts" in Path(key).parts
                 else self.settings.max_download_bytes)
        if len(response.content) > limit:
            raise ObjectStoreError("Azure 媒体超过下载大小限制")
        return response.content

    def exists(self, key: str) -> bool:
        mapping = self.db.media_objects.find_one({"_id": key, "status": "ready"})
        return mapping is not None

    def delete(self, key: str) -> None:
        self.db.media_objects.update_one(
            {"_id": key},
            {
                "$set": {
                    "status": "orphaned",
                    "orphaned_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )

    def presign(self, key: str, expires: int, download_name: str | None = None) -> str:
        del expires, download_name
        return str(self._ready_mapping(key)["url"])

    def public_url(self, key: str) -> str | None:
        mapping = self.db.media_objects.find_one({"_id": key, "status": "ready"})
        return str(mapping["url"]) if mapping else None

    def download_to_temp(self, key: str, suffix: str = "") -> Path:
        data = self.get_bytes(key)
        handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        return Path(handle.name)

    def local_path(self, key: str) -> Path | None:
        del key
        return None

    def check(self) -> None:
        if not self.settings.am_media_storage_service_key.strip():
            raise ObjectStoreError("未配置媒体存储服务身份")
        if not self.settings.am_base_url.strip():
            raise ObjectStoreError("未配置 AM Console 地址")

    def describe(self) -> str:
        return f"am-media:{self.merchant_id}"

    def _ready_mapping(self, key: str) -> dict[str, Any]:
        mapping = self.db.media_objects.find_one({"_id": key, "status": "ready"})
        if not mapping or not mapping.get("url"):
            raise ObjectStoreError("媒体对象不存在或尚未确认")
        return mapping

    @staticmethod
    def _kind(key: str) -> str:
        parts = {part.lower() for part in Path(key).parts}
        if "prepared" in parts:
            return "PREPARED"
        if "outputs" in parts or "artifacts" in parts:
            return "OUTPUTS"
        if "originals" in parts or "backgrounds" in parts:
            return "ORIGINALS"
        raise ObjectStoreError("无法从逻辑对象键确定媒体类型")


def create_merchant_store(settings: Settings, db: Any, am: AMClient, merchant_id: str) -> MediaObjectStore:
    return MediaObjectStore(settings, db, am, merchant_id)
