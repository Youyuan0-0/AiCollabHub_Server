from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.db.base import Base


def utc_now() -> datetime:
    """返回 UTC 本地处理时间，不作为 IAM 推送的来源版本。"""
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """统一 PostgreSQL 与隔离 SQLite 测试的 UTC 时间读写。"""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """写入前要求显式时区，避免本地时区被误认为 UTC。"""
        if value is not None and value.tzinfo is None:
            raise ValueError("A timezone-aware datetime is required")
        return value.astimezone(UTC) if value is not None else None

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """SQLite 不保存时区标记，读取时补回写入约定的 UTC。"""
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class UserStatus(StrEnum):
    """平台账号状态；待确认账号没有访问权限。"""

    PENDING = "pending"
    ACTIVE = "active"
    DISABLED = "disabled"
    DELETED = "deleted"


class UserRole(StrEnum):
    """平台准入角色，不包含 Mock 场景中的单位管理员。"""

    USER = "user"
    ADMIN = "admin"


class SessionRecordType(StrEnum):
    """区分正常登录会话与提前收到的票据撤销标记。"""

    SESSION = "session"
    TICKET_REVOCATION = "ticket_revocation"


class ActorType(StrEnum):
    """成功操作的发起方，IAM 同步不冒充被同步用户。"""

    USER = "user"
    SYSTEM = "system"
    IAM = "iam"


class OperationAction(StrEnum):
    """已确认的成功业务动作。"""

    LOGIN = "login"
    LOGOUT = "logout"
    SINGLE_LOGOUT = "single_logout"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    ENABLE = "enable"
    DISABLE = "disable"
    GRANT = "grant"
    REVOKE = "revoke"
    SYNC = "sync"
    INVOKE = "invoke"
    UPLOAD = "upload"
    DOWNLOAD = "download"
    REVIEW = "review"
    CONFIGURE = "configure"


class OperationTarget(StrEnum):
    """操作对象类别，具体对象编号由相应业务校验。"""

    USER = "user"
    SESSION = "session"
    IDENTITY_SYNC = "identity_sync"
    DATASET = "dataset"
    MODEL = "model"
    APPLICATION = "application"
    KNOWLEDGE_BASE = "knowledge_base"
    TOOL = "tool"
    CONVERSATION = "conversation"
    FEEDBACK = "feedback"
    FILE = "file"
    PLATFORM = "platform"


def enum_column(enum: type[StrEnum], name: str, length: int) -> Enum:
    """枚举保存小写业务值，以 VARCHAR 和 CHECK 兼容测试数据库。"""
    return Enum(
        enum,
        values_callable=lambda items: [item.value for item in items],
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        name=name,
        length=length,
    )


class User(Base):
    """保存集团最小用户资料及两类同步时间，供业务记录关联稳定 UUID。"""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("length(trim(iam_subject)) > 0", name="iam_subject"),
        CheckConstraint(
            "display_name IS NULL OR length(trim(display_name)) > 0", name="display_name"
        ),
        CheckConstraint("user_sync_time > 0", name="user_sync_time"),
        CheckConstraint("role_sync_time > 0", name="role_sync_time"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    iam_subject: Mapped[str] = mapped_column(String(200), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[UserStatus] = mapped_column(
        enum_column(UserStatus, "status", 16), default=UserStatus.PENDING, server_default="pending"
    )
    role: Mapped[UserRole] = mapped_column(
        enum_column(UserRole, "role", 16), default=UserRole.USER, server_default="user"
    )
    user_sync_time: Mapped[int | None] = mapped_column(BigInteger)
    role_sync_time: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utc_now, server_default=func.now()
    )


class AuthSession(Base):
    """保存共享会话和提前登出标记，数据库仅保留凭证摘要。"""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="lifetime"),
        CheckConstraint(
            "(record_type = 'session' AND user_id IS NOT NULL AND token_hash IS NOT NULL) OR "
            "(record_type = 'ticket_revocation' AND user_id IS NULL "
            "AND token_hash IS NULL AND revoked_at IS NOT NULL)",
            name="record",
        ),
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash").ddl_if(
            dialect="postgresql"
        ),
        CheckConstraint("ticket_hash ~ '^[0-9a-f]{64}$'", name="ticket_hash").ddl_if(
            dialect="postgresql"
        ),
        Index("ix_auth_sessions_expires_at", "expires_at"),
        Index(
            "ix_auth_sessions_active_user_id",
            "user_id",
            postgresql_where=text("record_type = 'session' AND revoked_at IS NULL"),
            sqlite_where=text("record_type = 'session' AND revoked_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    record_type: Mapped[SessionRecordType] = mapped_column(
        enum_column(SessionRecordType, "record_type", 24),
        default=SessionRecordType.SESSION,
        server_default="session",
    )
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    ticket_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utc_now, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    user: Mapped[User | None] = relationship(lazy="joined")


class OperationLog(Base):
    """只保存成功操作；失败、拒绝和重复请求的诊断进入系统日志。"""

    __tablename__ = "operation_logs"
    __table_args__ = (
        CheckConstraint(
            "(actor_type = 'user' AND user_id IS NOT NULL) OR "
            "(actor_type <> 'user' AND user_id IS NULL)",
            name="actor",
        ),
        CheckConstraint("jsonb_typeof(data) = 'object'", name="data").ddl_if(dialect="postgresql"),
        Index("ix_operation_logs_created_at", "created_at"),
        Index(
            "ix_operation_logs_user_id_created_at",
            "user_id",
            "created_at",
            postgresql_where=text("user_id IS NOT NULL"),
            sqlite_where=text("user_id IS NOT NULL"),
        ),
        Index(
            "ix_operation_logs_target_type_target_id_created_at",
            "target_type",
            "target_id",
            "created_at",
        ),
        Index(
            "ix_operation_logs_request_id",
            "request_id",
            postgresql_where=text("request_id IS NOT NULL"),
            sqlite_where=text("request_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    actor_type: Mapped[ActorType] = mapped_column(enum_column(ActorType, "actor_type", 16))
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    action: Mapped[OperationAction] = mapped_column(enum_column(OperationAction, "action", 40))
    target_type: Mapped[OperationTarget] = mapped_column(
        enum_column(OperationTarget, "target_type", 32)
    )
    target_id: Mapped[str | None] = mapped_column(String(200))
    request_id: Mapped[UUID | None] = mapped_column(Uuid)
    data: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=dict, server_default=text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utc_now, server_default=func.now()
    )
