from contextlib import ExitStack
from dataclasses import dataclass, field

from sqlalchemy import Engine

from app.cache.client import RedisCache
from app.core.config import Settings
from app.db.session import create_db_engine
from app.storage.client import MinioStorage


@dataclass
class ApplicationResources:
    """保存一个应用生命周期内的连接池与客户端，并统一管理回收顺序。

    多 worker 部署时各进程分别持有一份资源；请求复用这些客户端，但不能共享
    数据库 Session。字段为 None 表示未配置对应服务，由使用方决定是否允许缺省。
    """

    database: Engine | None = None
    cache: RedisCache | None = None
    storage: MinioStorage | None = None
    _cleanup: ExitStack = field(default_factory=ExitStack, repr=False)

    def close(self) -> None:
        """按注册顺序的逆序释放已创建的客户端资源。"""
        self._cleanup.close()


def create_resources(settings: Settings) -> ApplicationResources:
    """按配置创建独立客户端和连接池，不在启动时连接服务、建表或建桶。"""
    # 中途配置失败时，ExitStack 会回收此前已经创建的所有资源。
    with ExitStack() as cleanup:
        database = cache = storage = None
        if settings.database_url is not None:
            database = create_db_engine(
                settings.database_url.get_secret_value(),
                pool_size=settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
                pool_timeout=settings.db_pool_timeout,
                pool_recycle=settings.db_pool_recycle,
                connect_timeout=settings.db_connect_timeout,
                idle_session_timeout=settings.db_idle_session_timeout,
                application_name=f"aicollabhub:{settings.app_env}:{settings.instance_id}",
            )
            cleanup.callback(database.dispose)
        if settings.redis_url is not None:
            cache = RedisCache.from_settings(settings)
            cleanup.callback(cache.close)
        if settings.minio_endpoint is not None:
            storage = MinioStorage.from_settings(settings)
            cleanup.callback(storage.close)
        # 把清理职责转交给应用生命周期；不转移时，离开 with 就会关闭刚创建的客户端。
        return ApplicationResources(database, cache, storage, cleanup.pop_all())
