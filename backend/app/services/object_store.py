from __future__ import annotations

import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Protocol

from qcloud_cos import CosConfig, CosS3Client, CosServiceError

from app.config import Settings


logger = logging.getLogger(__name__)


class ObjectStoreError(Exception):
    pass


class ObjectStore(Protocol):
    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None: ...

    def get_bytes(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...

    def presign(self, key: str, expires: int, download_name: str | None = None) -> str: ...

    def download_to_temp(self, key: str, suffix: str = "") -> Path: ...

    def local_path(self, key: str) -> Path | None: ...

    def check(self) -> None: ...

    def describe(self) -> str: ...


class CosObjectStore:
    def __init__(
        self,
        settings: Settings,
        *,
        client: CosS3Client | None = None,
        cache_store: LocalObjectStore | None = None,
    ) -> None:
        if not settings.cos_enabled:
            raise ObjectStoreError("COS 配置不完整")
        config = CosConfig(
            Region=settings.cos_region,
            SecretId=settings.cos_secret_id,
            SecretKey=settings.cos_secret_key,
            Scheme="https",
        )
        self.settings = settings
        self.client = client or CosS3Client(config)
        self.cache = cache_store or LocalObjectStore(settings.storage_path / "objects")

    def put_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> None:
        self.cache.put_bytes(key, data, content_type)
        try:
            self.client.put_object(
                Bucket=self.settings.cos_bucket,
                Body=data,
                Key=key,
                ContentType=content_type,
            )
        except Exception as exc:  # noqa: BLE001 - local mirror keeps uploads usable
            logger.warning("COS upload failed; using local mirror for %s: %s", key, exc)

    def get_bytes(self, key: str) -> bytes:
        if self.cache.exists(key):
            return self.cache.get_bytes(key)
        try:
            response = self.client.get_object(
                Bucket=self.settings.cos_bucket,
                Key=key,
            )
            return response["Body"].get_raw_stream().read()
        except Exception as exc:  # noqa: BLE001
            raise ObjectStoreError(f"COS 读取失败：{exc}") from exc

    def exists(self, key: str) -> bool:
        if self.cache.exists(key):
            return True
        try:
            self.client.head_object(Bucket=self.settings.cos_bucket, Key=key)
            return True
        except CosServiceError as exc:
            if str(exc.get_status_code()) == "404":
                return False
            raise ObjectStoreError(f"COS 检查失败：{exc}") from exc

    def delete(self, key: str) -> None:
        self.cache.delete(key)
        try:
            self.client.delete_object(Bucket=self.settings.cos_bucket, Key=key)
        except CosServiceError as exc:
            if str(exc.get_status_code()) != "404":
                logger.warning("COS delete failed; local mirror was removed for %s: %s", key, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("COS delete failed; local mirror was removed for %s: %s", key, exc)

    def presign(
        self,
        key: str,
        expires: int,
        download_name: str | None = None,
    ) -> str:
        params = {}
        if download_name:
            params["response-content-disposition"] = (
                f'attachment; filename="{download_name}"'
            )
        return self.client.get_presigned_url(
            Bucket=self.settings.cos_bucket,
            Key=key,
            Method="GET",
            Expired=expires,
            Params=params,
        )

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
        path = self.cache.local_path(key)
        return path if path is not None and path.is_file() else None

    def check(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.settings.cos_bucket)
        except Exception as exc:  # noqa: BLE001
            raise ObjectStoreError(f"COS 不可用：{exc}") from exc

    def describe(self) -> str:
        return f"cos:{self.settings.cos_bucket}/{self.settings.cos_region}"


class LocalObjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ObjectStoreError("非法的对象键")
        return candidate

    def put_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> None:
        del content_type
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def get_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise ObjectStoreError("本地对象不存在")
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def presign(
        self,
        key: str,
        expires: int,
        download_name: str | None = None,
    ) -> str:
        del expires, download_name
        return f"file://{self._path(key)}"

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
        return self._path(key)

    def check(self) -> None:
        probe = self.root / ".healthcheck"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)

    def describe(self) -> str:
        return "local"


def create_object_store(settings: Settings) -> ObjectStore:
    if settings.cos_enabled:
        return CosObjectStore(settings)
    return LocalObjectStore(settings.storage_path / "objects")
