from contextlib import ExitStack
from dataclasses import dataclass, field

from app.cache.client import RedisCache
from app.core.config import Settings
from app.db.routing import DatabasePools
from app.storage.client import MinioStorage


@dataclass
class ApplicationResources:
    database: DatabasePools | None = None
    cache: RedisCache | None = None
    storage: MinioStorage | None = None
    _cleanup: ExitStack = field(default_factory=ExitStack, repr=False)

    def close(self) -> None:
        self._cleanup.close()


def create_resources(settings: Settings) -> ApplicationResources:
    # 客户端均延迟连接；启动不访问外部服务，也不自动建表、建桶或切换主备。
    # 中途配置失败时，ExitStack 会回收此前已经创建的所有资源。
    with ExitStack() as cleanup:
        database = cache = storage = None
        if settings.database_url is not None:
            database = DatabasePools.from_settings(settings)
            cleanup.callback(database.close)
        if settings.redis_url is not None:
            cache = RedisCache.from_settings(settings)
            cleanup.callback(cache.close)
        if settings.minio_endpoint is not None:
            storage = MinioStorage.from_settings(settings)
            cleanup.callback(storage.close)
        return ApplicationResources(database, cache, storage, cleanup.pop_all())
