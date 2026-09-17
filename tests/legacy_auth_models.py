"""冻结 20260916_01 认证表定义，仅用于验证历史迁移；应用不得导入。"""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    MetaData,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.base import Base as ApplicationBase


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=ApplicationBase.metadata.naming_convention)


class AuthSession(Base):
    """保存业务登录会话的摘要、账号、有效期与注销状态。"""

    __tablename__ = "auth_session"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    ticket_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # 新会话共用 platform 范围；user/admin 仅兼容旧会话，slo 用于提前登出的撤销标记。
    scope: Mapped[str] = mapped_column(String(16))
    # 提前到达的登出通知也保留撤销标记，阻止随后完成验票的回调重新建立会话。
    subject: Mapped[str | None] = mapped_column(String(200), index=True)
    display_name: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[int] = mapped_column(BigInteger, index=True)
    revoked_at: Mapped[int | None] = mapped_column(BigInteger)


class AuthLoginRequest(Base):
    """保存一次性登录请求，用于绑定浏览器与认证回调。"""

    __tablename__ = "auth_login_request"

    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    browser_hash: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(16))
    service_url: Mapped[str] = mapped_column(Text)
    return_path: Mapped[str] = mapped_column(String(2048))
    created_at: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[int] = mapped_column(BigInteger, index=True)
    consumed_at: Mapped[int | None] = mapped_column(BigInteger)


class AuthEventLog(Base):
    """记录认证事件及结果，供登录审计使用。"""

    __tablename__ = "auth_event_log"
    __table_args__ = (Index("ix_auth_event_log_subject_created", "subject", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    subject: Mapped[str | None] = mapped_column(String(200))
    scope: Mapped[str] = mapped_column(String(16))
    event: Mapped[str] = mapped_column(String(40))
    result: Mapped[str] = mapped_column(String(40))
    request_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[int] = mapped_column(BigInteger, index=True)


class IdentityState(Base):
    """保存身份状态及其源版本，为登录和用户状态推送提供共同锁定点。"""

    __tablename__ = "iam_identity_state"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled', 'deleted')", name="valid_status"),
        CheckConstraint("sync_time >= 0", name="valid_sync_time"),
    )

    subject: Mapped[str] = mapped_column(String(200), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    # 0 表示尚未收到用户状态推送，仅在模拟环境允许沿用已验票的身份。
    sync_time: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)


class IdentityRole(Base):
    """保存身份的角色授权与独立源版本，保留撤权记录以防旧配置恢复权限。"""

    __tablename__ = "iam_identity_role"
    __table_args__ = (CheckConstraint("sync_time > 0", name="valid_sync_time"),)

    subject: Mapped[str] = mapped_column(
        ForeignKey("iam_identity_state.subject"),
        primary_key=True,
    )
    role_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    authorized: Mapped[bool] = mapped_column(Boolean)
    sync_time: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)


class IdentitySyncReceipt(Base):
    """保存同步事件指纹、处理结果和重试次数，支持幂等核对。"""

    __tablename__ = "iam_sync_receipt"

    event_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(32))
    sync_time: Mapped[int] = mapped_column(BigInteger)
    applied: Mapped[int] = mapped_column(default=0)
    ignored: Mapped[int] = mapped_column(default=0)
    unchanged: Mapped[int] = mapped_column(default=0)
    revoked_sessions: Mapped[int] = mapped_column(default=0)
    attempts: Mapped[int] = mapped_column(default=1)
    received_at: Mapped[int] = mapped_column(BigInteger, index=True)
    last_received_at: Mapped[int] = mapped_column(BigInteger)


def historical_metadata():
    """保留业务表并替换认证部分，避免历史迁移测试被当前 ORM 定义污染。"""
    metadata = MetaData(naming_convention=ApplicationBase.metadata.naming_convention)
    for table in ApplicationBase.metadata.tables.values():
        if table.name not in {"users", "auth_sessions", "operation_logs", "iam_sync_receipts"}:
            table.to_metadata(metadata)
    for table in Base.metadata.tables.values():
        table.to_metadata(metadata)
    return metadata


def check_historical(engine):
    """直接比较历史结构，不要求旧版本测试库升级到最新 head。"""
    metadata = historical_metadata()
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={
                "include_object": lambda obj, name, kind, reflected, target: (
                    not (kind == "table" and reflected and target is None)
                )
            },
        )
        assert compare_metadata(context, metadata) == []
