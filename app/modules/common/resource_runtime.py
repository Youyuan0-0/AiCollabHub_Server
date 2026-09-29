"""外部调用的短期协调状态，正式资源和业务记录仍存 PostgreSQL。"""

from __future__ import annotations

import json
from hashlib import sha256
from itertools import batched

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

    def get_many(self, kind: str, keys: list[str]) -> dict[str, dict | None]:
        """按明确的业务编号分批读取运行态；缺失保留空值，读取失败仍明确报错。"""
        try:
            result = {}
            for batch in batched(dict.fromkeys(keys), 100):
                cache_keys = [
                    self._prefix(kind) + sha256(key.encode()).hexdigest() for key in batch
                ]
                values = self.cache.client.mget(cache_keys)
                result.update(
                    (key, json.loads(value) if value else None)
                    for key, value in zip(batch, values, strict=True)
                )
            return result
        except (RedisError, ValueError, TypeError) as error:
            raise HTTPException(503, "runtime_state_unavailable") from error

    def list(self, kind: str) -> list[dict]:
        """分批读取仍在保留期内的模拟结果，避免逐条 GET 累积网络等待。"""
        try:
            result = []
            keys = self.cache.client.scan_iter(match=self._prefix(kind) + "*", count=100)
            seen = set()
            for batch in batched(keys, 100):
                unique = [key for key in dict.fromkeys(batch) if key not in seen]
                seen.update(unique)
                if unique:
                    result.extend(
                        json.loads(raw) for raw in self.cache.client.mget(unique) if raw is not None
                    )
            return result
        except (RedisError, ValueError, TypeError) as error:
            raise HTTPException(503, "runtime_state_unavailable") from error

    def mark_notifications_read(self, recipient: str, ids: list[str]) -> list[str]:
        """原子校验一批通知的归属后标记已读，保持原 TTL，不复活过期通知。"""
        ids = list(dict.fromkeys(ids))
        if not 1 <= len(ids) <= 100:
            raise HTTPException(422, "每批请选择 1 到 100 条消息")
        script = """
        local entries = {}
        for i, key in ipairs(KEYS) do
            local raw = redis.call('GET', key)
            if not raw then return 0 end
            local item = cjson.decode(raw)
            if item.recipient ~= ARGV[1] or
                (item.kind ~= 'review' and item.kind ~= 'new_version') then return 0 end
            entries[i] = item
        end
        for i, key in ipairs(KEYS) do
            entries[i].read = true
            redis.call('SET', key, cjson.encode(entries[i]), 'KEEPTTL')
        end
        return 1
        """
        keys = [self._prefix("notifications") + sha256(key.encode()).hexdigest() for key in ids]
        try:
            result = self.cache.client.eval(script, len(keys), *keys, recipient)
        except (RedisError, ValueError, TypeError) as error:
            raise HTTPException(503, "runtime_state_unavailable") from error
        if result != 1:
            raise HTTPException(404, "消息已过期或无权访问，请刷新列表")
        return ids
