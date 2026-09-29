"""已确认资源基线的公共访问；只读取现有表，不在业务启动时建表。"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
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

# 元组依次表示资源主表名、发布申请/版本表前缀、业务模块标识。
# 知识库和工具的数据库命名与接口资源类型不同，统一在此映射，调用方不要自行拼主表名。
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
        """为一次业务操作提供 Session，并在退出时提交写入或结束只读快照。

        使用 Engine 时，只读事务采用 REPEATABLE READ，使一次聚合读取中的列表、
        计数和关联资料保持同一快照。传入已有 Connection 时沿用其事务配置，
        通过保存点支持测试的外层回滚，不替调用方修改外层隔离级别。

        写事务沿用统一超时和提交规则；唯一约束、序列化、死锁及锁等待冲突映射为
        409。提交结果不确定的 503 由 commit_write 处理，此处不自动重放业务。
        """
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
        # 映射仅在本次 Session 内复用，避免跨请求缓存身份变更后的旧资料。
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

    def prefetch_users(self, session: Session, user_ids: Iterable[UUID]) -> None:
        """批量补齐当前事务的用户显示资料，不跨请求复用身份信息。"""
        cache = session.info.setdefault("user_views", {})
        missing = set(user_ids) - cache.keys()
        if not missing:
            return
        users = self.table("users")
        for row in session.execute(select(users).where(users.c.id.in_(missing))).mappings():
            cache[row["id"]] = {
                "id": row["iam_subject"],
                "name": row["display_name"] or row["iam_subject"],
            }

    @staticmethod
    def lock(session: Session, key: str) -> None:
        """按业务键取得 PostgreSQL 事务级咨询锁，保护首次创建和后续修改。

        key 应包含业务类型及资源、用户或请求编号，让竞争同一操作的 worker 使用
        相同键。锁随事务结束自动释放；即使目标行尚不存在，也能串行化幂等检查。
        """
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": "foundation:" + key},
        )
