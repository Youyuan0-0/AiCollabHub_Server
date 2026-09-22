"""外部调用的短期协调状态，正式资源和业务记录仍存 PostgreSQL。"""

import json
from hashlib import sha256

from fastapi import HTTPException
from redis.exceptions import RedisError

from app.cache.client import RedisCache


class ResourceRuntime:
    """使用 Redis 共享外部模拟运行、幂等结果和临时通知，过期后不冒充持久历史。"""

    def __init__(self, cache: RedisCache, module: str, *, ttl: int = 86400) -> None:
        self.cache = cache
        self.module = module
        self.ttl = ttl

    def _prefix(self, kind: str) -> str:
        return f"{self.cache.namespace}:runtime:{self.module}:{kind}:"

    def get(self, kind: str, key: str) -> dict | None:
        """缓存不可用时明确失败，不能把读取失败当成未执行而重复调用外部接口。"""
        try:
            value = self.cache.client.get(self._prefix(kind) + sha256(key.encode()).hexdigest())
            return json.loads(value) if value else None
        except (RedisError, ValueError, TypeError) as error:
            raise HTTPException(503, "runtime_state_unavailable") from error

    def put(self, kind: str, key: str, value: dict) -> None:
        """每个请求单独设置期限，不因其他请求续期而永久保留旧结果。"""
        try:
            self.cache.client.set(
                self._prefix(kind) + sha256(key.encode()).hexdigest(),
                json.dumps(value, ensure_ascii=False, allow_nan=False),
                ex=self.ttl,
            )
        except (RedisError, ValueError, TypeError) as error:
            raise HTTPException(503, "runtime_state_unavailable") from error

    def list(self, kind: str) -> list[dict]:
        """读取仍在保留期内的模拟结果，供现有开发运行视图使用。"""
        try:
            result = []
            for key in self.cache.client.scan_iter(match=self._prefix(kind) + "*", count=100):
                raw = self.cache.client.get(key)
                if raw is not None:
                    result.append(json.loads(raw))
            return result
        except (RedisError, ValueError, TypeError) as error:
            raise HTTPException(503, "runtime_state_unavailable") from error
