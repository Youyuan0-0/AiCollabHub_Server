from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated, BinaryIO, NoReturn
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from minio import Minio
from minio.error import MinioException, S3Error
from minio.helpers import check_bucket_name
from urllib3 import PoolManager, Timeout
from urllib3.exceptions import HTTPError
from urllib3.response import HTTPResponse
from urllib3.util.retry import Retry

from app.core.config import Settings

logger = logging.getLogger(__name__)


class StorageUnavailable(RuntimeError):
    """对象存储不可用或无法取得可信元信息。"""

    pass


class ObjectNotFound(RuntimeError):
    """指定对象不存在，供业务层转换为资源缺失响应。"""

    pass


@dataclass(frozen=True)
class ObjectInfo:
    """对象的内部键、大小、摘要、内容类型及版本信息。"""

    key: str
    size: int
    etag: str | None
    content_type: str | None
    version_id: str | None = None


class MinioStorage:
    """封装对象存储访问、大小限制、异常转换与连接释放。"""

    def __init__(
        self, client: Minio, *, bucket: str, http: PoolManager, max_upload_bytes: int
    ) -> None:
        self.client = client
        self.bucket = bucket
        self.http = http
        self.max_upload_bytes = max_upload_bytes

    @classmethod
    def from_settings(cls, settings: Settings) -> MinioStorage:
        """从受控配置创建存储客户端，初始化失败时释放连接池。"""
        if settings.minio_endpoint is None:
            raise ValueError("MINIO_ENDPOINT is required")
        check_bucket_name(settings.minio_bucket, strict=True)
        endpoint = urlsplit(str(settings.minio_endpoint))
        http = PoolManager(
            timeout=Timeout(connect=settings.minio_timeout, read=settings.minio_timeout),
            retries=Retry(total=0, connect=0, read=0, redirect=0),
            maxsize=10,
        )
        try:
            client = Minio(
                endpoint.netloc,
                access_key=settings.minio_access_key.get_secret_value(),
                secret_key=settings.minio_secret_key.get_secret_value(),
                secure=endpoint.scheme == "https",
                region=settings.minio_region,
                http_client=http,
            )
            return cls(
                client,
                bucket=settings.minio_bucket,
                http=http,
                max_upload_bytes=settings.minio_max_upload_bytes,
            )
        except BaseException:
            http.clear()
            raise

    @staticmethod
    def _validate_key(key: str) -> None:
        if not key or "\\" in key or any(ord(char) < 32 for char in key):
            raise ValueError("Invalid object key")
        if any(part in ("", ".", "..") for part in key.split("/")):
            raise ValueError("Object key must use nonempty relative path segments")

    @staticmethod
    def _translate_error(error: Exception) -> NoReturn:
        if isinstance(error, S3Error) and error.code in {"NoSuchKey", "NoSuchObject"}:
            raise ObjectNotFound("object_not_found") from None
        # 不把对象地址、签名或 SDK 的原始响应写入日志和业务异常。
        logger.warning("Object storage request failed (%s)", type(error).__name__)
        raise StorageUnavailable("object_storage_unavailable") from None

    def put_object(
        self,
        key: str,
        data: BinaryIO,
        *,
        length: int,
        content_type: str = "application/octet-stream",
    ) -> ObjectInfo:
        """校验对象键和上传大小后写入原始数据并返回元信息。"""
        self._validate_key(key)
        if isinstance(length, bool) or not isinstance(length, int):
            raise ValueError("Object length must be an integer")
        if not 0 <= length <= self.max_upload_bytes:
            raise ValueError("Object length is outside the upload limit")
        try:
            result = self.client.put_object(
                self.bucket, key, data, length, content_type=content_type
            )
            return ObjectInfo(key, length, result.etag, content_type, result.version_id)
        except (MinioException, HTTPError, OSError) as error:
            self._translate_error(error)

    def stat_object(self, key: str) -> ObjectInfo:
        """读取对象元信息，缺失或无法确认大小时抛出明确异常。"""
        self._validate_key(key)
        try:
            result = self.client.stat_object(self.bucket, key)
            if result.size is None:
                raise StorageUnavailable("object_metadata_unavailable")
            return ObjectInfo(key, result.size, result.etag, result.content_type, result.version_id)
        except (MinioException, HTTPError, OSError) as error:
            self._translate_error(error)

    @contextmanager
    def open_object(self, key: str, *, offset: int = 0, length: int = 0) -> Iterator[HTTPResponse]:
        """打开对象或指定字节范围，读取结束或中断后归还连接。"""
        self._validate_key(key)
        if offset < 0 or length < 0:
            raise ValueError("Invalid object range")
        try:
            options = {"offset": offset, "length": length} if offset or length else {}
            response = self.client.get_object(self.bucket, key, **options)
        except (MinioException, HTTPError, OSError) as error:
            self._translate_error(error)
        try:
            yield response
        finally:
            # 调用方中断读取也必须释放底层 HTTP 连接，避免长期耗尽文件连接池。
            try:
                response.close()
            finally:
                response.release_conn()

    def remove_object(self, key: str) -> None:
        """校验内部对象键后删除指定对象。"""
        self._validate_key(key)
        try:
            self.client.remove_object(self.bucket, key)
        except (MinioException, HTTPError, OSError) as error:
            self._translate_error(error)

    def probe(self) -> bool:
        """检查目标存储桶是否可访问，失败时返回不可用状态。"""
        try:
            return self.client.bucket_exists(self.bucket)
        except (MinioException, HTTPError, OSError) as error:
            logger.warning("Object storage probe failed (%s)", type(error).__name__)
            return False

    def close(self) -> None:
        """释放对象存储 HTTP 连接池。"""
        self.http.clear()


def get_storage(request: Request) -> MinioStorage:
    """取得应用共享存储客户端，未配置时返回服务不可用。"""
    resources = request.app.state.resources
    if resources is None or resources.storage is None:
        raise HTTPException(503, detail="object_storage_not_configured")
    return resources.storage


# 后续文件业务先校验 fileId 的权限，再把内部 object key 交给此客户端。
StorageDependency = Annotated[MinioStorage, Depends(get_storage)]
