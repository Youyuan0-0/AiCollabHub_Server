import json
import logging
from typing import Annotated, Any

from fastapi import Depends, Request
from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

from app.core.config import Settings

logger = logging.getLogger(__name__)


class RedisCache:
    def __init__(self, client: Redis, *, namespace: str, default_ttl: int):
        self.client = client
        self.namespace = namespace
        self.default_ttl = default_ttl

    @classmethod
    def from_settings(cls, settings: Settings) -> "RedisCache":
        if settings.redis_url is None:
            raise ValueError("REDIS_URL is required")
        client = Redis.from_url(
            settings.redis_url.get_secret_value(),
            decode_responses=True,
            socket_connect_timeout=settings.redis_timeout,
            socket_timeout=settings.redis_timeout,
            max_connections=settings.redis_max_connections,
            retry=Retry(NoBackoff(), 0),
        )
        return cls(
            client,
            namespace=f"{settings.redis_key_prefix}:{settings.app_env}",
            default_ttl=settings.redis_default_ttl,
        )

    def _key(self, key: str) -> str:
        if not key or not key.strip():
            raise ValueError("Cache key cannot be empty")
        return f"{self.namespace}:{key}"

    def get_json(self, key: str) -> Any | None:
        try:
            value = self.client.get(self._key(key))
        except UnicodeError:
            logger.warning("Invalid cached text encoding")
            self.delete(key)
            return None
        except RedisError as error:
            logger.warning("Cache read failed (%s)", type(error).__name__)
            return None
        if value is None:
            return None
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            # 损坏的缓存按未命中处理；不把序列化错误扩散成数据库业务失败。
            logger.warning("Invalid cached JSON")
            self.delete(key)
            return None

    def set_json(self, key: str, value: Any, *, ttl: int | None = None) -> bool:
        ttl = self.default_ttl if ttl is None else ttl
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
            raise ValueError("Cache TTL must be a positive integer")
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
        try:
            return bool(self.client.set(self._key(key), payload, ex=ttl))
        except RedisError as error:
            logger.warning("Cache write failed (%s)", type(error).__name__)
            return False

    def delete(self, *keys: str) -> bool:
        if not keys:
            return True
        try:
            self.client.delete(*(self._key(key) for key in keys))
            return True
        except RedisError as error:
            logger.warning("Cache invalidation failed (%s)", type(error).__name__)
            return False

    def probe(self) -> bool:
        try:
            return bool(self.client.ping())
        except RedisError as error:
            logger.warning("Cache probe failed (%s)", type(error).__name__)
            return False

    def close(self) -> None:
        self.client.close()


def get_cache(request: Request) -> RedisCache | None:
    resources = request.app.state.resources
    return resources.cache if resources is not None else None


# 缓存是可选加速层；业务权威数据仍由数据库保存，调用者须处理 None 和写入失败。
CacheDependency = Annotated[RedisCache | None, Depends(get_cache)]
