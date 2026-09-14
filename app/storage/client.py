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
    pass


class ObjectNotFound(RuntimeError):
    pass


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    etag: str | None
    content_type: str | None
    version_id: str | None = None


class MinioStorage:
    def __init__(self, client: Minio, *, bucket: str, http: PoolManager, max_upload_bytes: int):
        self.client = client
        self.bucket = bucket
        self.http = http
        self.max_upload_bytes = max_upload_bytes

    @classmethod
    def from_settings(cls, settings: Settings) -> "MinioStorage":
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
        self._validate_key(key)
        try:
            result = self.client.stat_object(self.bucket, key)
            if result.size is None:
                raise StorageUnavailable("object_metadata_unavailable")
            return ObjectInfo(key, result.size, result.etag, result.content_type, result.version_id)
        except (MinioException, HTTPError, OSError) as error:
            self._translate_error(error)

    @contextmanager
    def open_object(self, key: str) -> Iterator[HTTPResponse]:
        self._validate_key(key)
        try:
            response = self.client.get_object(self.bucket, key)
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
        self._validate_key(key)
        try:
            self.client.remove_object(self.bucket, key)
        except (MinioException, HTTPError, OSError) as error:
            self._translate_error(error)

    def probe(self) -> bool:
        try:
            return self.client.bucket_exists(self.bucket)
        except (MinioException, HTTPError, OSError) as error:
            logger.warning("Object storage probe failed (%s)", type(error).__name__)
            return False

    def close(self) -> None:
        self.http.clear()


def get_storage(request: Request) -> MinioStorage:
    resources = request.app.state.resources
    if resources is None or resources.storage is None:
        raise HTTPException(503, detail="object_storage_not_configured")
    return resources.storage


# 后续文件业务先校验 fileId 的权限，再把内部 object key 交给此客户端。
StorageDependency = Annotated[MinioStorage, Depends(get_storage)]
