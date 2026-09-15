"""场景应用的 PostgreSQL 仓储；复用主库连接池，不自动建表或导入样例。"""

from contextlib import contextmanager
from threading import RLock

from fastapi import HTTPException
from sqlalchemy import MetaData, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db.transactions import commit_write, prepare_write
from app.modules.applications.database_session import DatabaseApplicationSession
from app.modules.applications.demo_marketplace import DemoMarketplaceStore
from app.modules.applications.sample_data import seed_applications

TABLE_NAMES = (
    "scene_application",
    "scene_visibility_target",
    "scene_interaction",
    "scene_submission_extension",
    "scene_activity",
    "scene_review_note",
    "scene_notification",
    "scene_configuration",
)


class DatabaseApplicationRepository:
    def __init__(self, engine, settings, marketplace: DemoMarketplaceStore):
        self.engine = engine
        self.settings = settings
        self.marketplace = marketplace
        self.seeds = {item["id"]: item for item in seed_applications()}
        self._tables = None
        self._reflection_lock = RLock()

    def tables(self):
        # 延迟到业务首次访问；反射只读取已存在的表，缺表或连接失败时不回退内存。
        with self._reflection_lock:
            if self._tables is None:
                if self.engine.dialect.name != "postgresql":
                    raise HTTPException(503, "application_storage_requires_postgresql")
                metadata = MetaData(schema="public")
                try:
                    metadata.reflect(bind=self.engine, only=TABLE_NAMES, resolve_fks=False)
                except DBAPIError:
                    raise
                except Exception:
                    raise HTTPException(503, "application_database_schema_not_ready") from None
                self._tables = {name: metadata.tables[f"public.{name}"] for name in TABLE_NAMES}
            return self._tables

    @contextmanager
    def transaction(self, *, readonly=False):
        tables = self.tables()
        # 同一快照内组合资料、权限和统计，避免读请求之间互相加写锁。
        engine = (
            self.engine.execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            )
            if readonly
            else self.engine
        )
        with Session(engine, autoflush=False, expire_on_commit=False) as database:
            try:
                if readonly:
                    database.execute(
                        text(
                            "SELECT set_config('statement_timeout', :timeout, true), "
                            "set_config('lock_timeout', '3000', true)"
                        ),
                        {"timeout": str(self.settings.db_statement_timeout_ms)},
                    )
                else:
                    prepare_write(database, self.settings)
                session = DatabaseApplicationSession(self, database, tables, readonly=readonly)
                yield session
                if session.dirty:
                    commit_write(database)
                else:
                    database.rollback()
            except DBAPIError as error:
                database.rollback()
                code = getattr(error.orig, "sqlstate", None)
                if code in ("23505", "40001", "40P01", "55P03"):
                    raise HTTPException(409, "记录已变化或正在处理中，请刷新后重试") from None
                raise
