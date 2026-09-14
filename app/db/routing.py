import logging
import random
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import Engine, event, literal, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import ORMExecuteState, Session

from app.core.config import Settings
from app.db.session import create_db_engine

logger = logging.getLogger(__name__)


class ReadOnlyViolation(RuntimeError):
    """只读依赖收到写操作或无法确认安全的 SQL。"""


class ReadDatabaseUnavailable(RuntimeError):
    pass


class ReadOnlySession(Session):
    pass


@event.listens_for(ReadOnlySession, "before_flush")
def reject_flush(session, flush_context, instances):
    if session.new or session.dirty or session.deleted:
        raise ReadOnlyViolation("A read-only session cannot flush changes")


@event.listens_for(ReadOnlySession, "do_orm_execute")
def reject_write_statement(state: ORMExecuteState):
    # 不用 SQL 前缀猜测读写。文本 SQL、写语句和行锁必须使用主库依赖。
    if not state.is_select or getattr(state.statement, "_for_update_arg", None) is not None:
        raise ReadOnlyViolation("A read-only session accepts only unlocked SQLAlchemy selects")


@dataclass
class DatabasePools:
    primary: Engine
    replica: Engine | None = None
    fallback: Engine | None = None
    read_enabled: bool = False
    read_weight: int = 100
    _cleanup: ExitStack = field(default_factory=ExitStack, repr=False)

    @classmethod
    def from_settings(cls, settings: Settings) -> "DatabasePools":
        if settings.database_url is None:
            raise ValueError("DATABASE_URL is required")
        options = {
            "pool_size": settings.db_pool_size,
            "max_overflow": settings.db_max_overflow,
            "pool_timeout": settings.db_pool_timeout,
            "pool_recycle": settings.db_pool_recycle,
            "connect_timeout": settings.db_connect_timeout,
        }
        with ExitStack() as cleanup:
            primary = create_db_engine(
                settings.database_url.get_secret_value(), target_role="primary", **options
            )
            cleanup.callback(primary.dispose)
            replica = fallback = None
            if settings.db_read_enabled:
                replica = create_db_engine(
                    settings.database_read_url.get_secret_value(),
                    target_role="standby",
                    readonly=True,
                    **options,
                )
                cleanup.callback(replica.dispose)
            if settings.db_read_fallback_enabled:
                fallback = create_db_engine(
                    settings.database_read_fallback_url.get_secret_value(),
                    target_role="primary",
                    readonly=True,
                    **options,
                )
                cleanup.callback(fallback.dispose)
            return cls(
                primary,
                replica,
                fallback,
                settings.db_read_enabled,
                settings.db_read_weight,
                cleanup.pop_all(),
            )

    def choose_read_engine(self) -> Engine:
        if self.read_enabled and self.replica is not None:
            if random.randrange(100) < self.read_weight:
                return self.replica
        return self.primary

    @staticmethod
    def _connect_readonly(engine: Engine):
        connection = engine.connect()
        try:
            if engine.dialect.name == "postgresql":
                connection = connection.execution_options(postgresql_readonly=True)
            return connection
        except BaseException:
            connection.close()
            raise

    @contextmanager
    def read_session(self) -> Iterator[ReadOnlySession]:
        engine = self.choose_read_engine()
        try:
            connection = self._connect_readonly(engine)
        except SQLAlchemyError as error:
            logger.warning("Read connection failed (%s)", type(error).__name__)
            if engine is not self.replica or self.fallback is None:
                raise ReadDatabaseUnavailable("read_database_unavailable") from None
            try:
                connection = self._connect_readonly(self.fallback)
            except SQLAlchemyError:
                raise ReadDatabaseUnavailable("read_database_unavailable") from None
        # 回退仅发生在获取连接失败时；查询开始后的错误不重放、不跨库重试。
        try:
            with ReadOnlySession(
                bind=connection, autoflush=False, expire_on_commit=False
            ) as session:
                yield session
        finally:
            connection.close()

    @staticmethod
    def probe(engine: Engine) -> bool:
        try:
            with engine.connect() as connection:
                return connection.scalar(select(literal(1))) == 1
        except SQLAlchemyError as error:
            logger.warning("Database probe failed (%s)", type(error).__name__)
            return False

    def close(self) -> None:
        self._cleanup.close()


def get_read_session(request: Request) -> Iterator[ReadOnlySession]:
    resources = request.app.state.resources
    if resources is None or resources.database is None:
        raise HTTPException(503, detail="database_not_configured")
    try:
        with resources.database.read_session() as session:
            yield session
    except ReadDatabaseUnavailable:
        raise HTTPException(503, detail="read_database_unavailable") from None


# 仅供业务已确认可以分流的纯查询使用，普通 SessionDependency 始终走主库。
ReadSessionDependency = Annotated[ReadOnlySession, Depends(get_read_session)]
