"""已确认资源基线的公共访问；只读取现有表，不在业务启动时建表。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import RLock
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import JSON, Connection, Engine, MetaData, Table, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.transactions import commit_write, prepare_write

RESOURCE_TABLES = {
    "dataset": ("datasets", "dataset", "datasets"),
    "model": ("models", "model", "models"),
    "application": ("applications", "application", "applications"),
    "knowledge": ("knowledge_bases", "knowledge_base", "knowledge"),
    "tool": ("ai_tools", "ai_tool", "tools"),
}

COMMON_TABLES = (
    "users",
    "auth_sessions",
    "iam_sync_receipts",
    "operation_logs",
    "system_logs",
    "resource_ratings",
    "resource_favorites",
    "resource_follows",
    "resource_downloads",
    "resource_reviews",
    "files",
    "feedback_records",
    "feedback_events",
    "feedback_attachments",
    "application_dependencies",
    "module_configurations",
    "resource_visibility_policies",
    "resource_visibility_targets",
    "resource_file_references",
    "file_previews",
    "conversations",
    "conversation_messages",
    "conversation_message_attachments",
    "home_recommendations",
    "home_question_suggestions",
    "announcements",
    "knowledge_documents",
    "knowledge_document_reviews",
)


def identifier(value: str | UUID) -> UUID:
    """将接口编号解析为平台 UUID，无效编号按资源不存在处理。"""
    try:
        return value if isinstance(value, UUID) else UUID(value)
    except (ValueError, TypeError, AttributeError) as error:
        raise HTTPException(404, "资源不存在") from error


def utc_now() -> datetime:
    """生成数据库和接口共用的带时区时间。"""
    return datetime.now(UTC)


def date_value(value: str | datetime | None) -> datetime | None:
    """转换接口时间，空值仍保留空值。"""
    if value is None or value == "":
        return None
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def iso(value: datetime | None) -> str | None:
    """序列化数据库时间，不为未知时间生成虚构值。"""
    return value.isoformat() if value is not None else None


class FoundationDatabase:
    """复用应用连接池，为模块提供新基线表、用户映射和原子事务。"""

    def __init__(
        self, engine: Engine | Connection, settings: Settings, *, schema: str | None = None
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.metadata = MetaData(schema=schema)
        self._tables: dict[str, Table] = {}
        self._lock = RLock()

    def warm_up(self) -> None:
        """启动时用一个连接读取表结构，避免冷请求持有事务时再次等待连接池。"""
        names = list(COMMON_TABLES)
        for main, prefix, _ in RESOURCE_TABLES.values():
            names.extend((main, prefix + "_publications", prefix + "_versions"))
        with self._lock:
            self.metadata.reflect(bind=self.engine, only=names, resolve_fks=False)
            for name in names:
                key = f"{self.metadata.schema}.{name}" if self.metadata.schema else name
                table = self.metadata.tables[key]
                for column in table.columns:
                    if column.nullable and isinstance(column.type, JSON):
                        column.type.none_as_null = True
                self._tables[name] = table

    def table(self, name: str) -> Table:
        """按代码中明确的表名惰性读取结构，缺表时让配置错误明确暴露。"""
        with self._lock:
            if name not in self._tables:
                self._tables[name] = Table(
                    name, self.metadata, autoload_with=self.engine, resolve_fks=False
                )
                # 可空 JSON 的 None 表示 SQL NULL，避免反射默认把它写成 JSON null。
                for column in self._tables[name].columns:
                    if column.nullable and isinstance(column.type, JSON):
                        column.type.none_as_null = True
            return self._tables[name]

    @contextmanager
    def transaction(self, *, is_readonly: bool = False) -> Iterator[Session]:
        """一次业务调用使用同一事务；只读快照不提交，写入保留超时和不确定提交处理。"""
        engine = (
            self.engine.execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            )
            if is_readonly and isinstance(self.engine, Engine)
            else self.engine
        )
        with Session(
            engine,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        ) as session:
            try:
                if not is_readonly:
                    prepare_write(session, self.settings)
                yield session
                if is_readonly:
                    session.rollback()
                else:
                    commit_write(session)
            except DBAPIError as error:
                session.rollback()
                if getattr(error.orig, "sqlstate", None) in {"23505", "40001", "40P01", "55P03"}:
                    raise HTTPException(409, "记录已变化或正在处理中，请刷新后重试") from None
                raise

    def user_id(self, session: Session, subject: str) -> UUID:
        """将可信 IAM 身份映射到已建立的本地用户，不在业务中创建虚构用户。"""
        cache = session.info.setdefault("subject_ids", {})
        if subject in cache:
            return cache[subject]
        users = self.table("users")
        value = session.scalar(select(users.c.id).where(users.c.iam_subject == subject))
        if value is None:
            raise HTTPException(401, "authentication_required")
        cache[subject] = value
        return value

    def user(self, session: Session, user_id: UUID) -> dict:
        """读取显示资料；返回 IAM 标识以兼容现有前端，数据库关联仍使用 UUID。"""
        cache = session.info.setdefault("user_views", {})
        if user_id in cache:
            return cache[user_id]
        users = self.table("users")
        row = session.execute(select(users).where(users.c.id == user_id)).mappings().one()
        cache[user_id] = {
            "id": row["iam_subject"],
            "name": row["display_name"] or row["iam_subject"],
        }
        return cache[user_id]

    @staticmethod
    def lock(session: Session, key: str) -> None:
        """锁定包含首次创建的业务键，避免不存在记录时行锁无法保护幂等操作。"""
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": "foundation:" + key},
        )
