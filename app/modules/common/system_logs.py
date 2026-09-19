"""系统运行事件与异常的持久化模型。"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import JSON, CheckConstraint, Index, String, Text, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.modules.common.auth.models import UtcDateTime, enum_column, utc_now


class SystemLogLevel(StrEnum):
    """系统日志级别，数据库统一保存小写值。"""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class SystemLog(Base):
    """记录技术运维事件；同一请求可产生多条日志，后台事件可不关联请求。"""

    __tablename__ = "system_logs"
    __table_args__ = (
        CheckConstraint("jsonb_typeof(details) = 'object'", name="details").ddl_if(
            dialect="postgresql"
        ),
        Index("ix_system_logs_created_at", "created_at"),
        Index(
            "ix_system_logs_request_id",
            "request_id",
            postgresql_where=text("request_id IS NOT NULL"),
            sqlite_where=text("request_id IS NOT NULL"),
        ),
        {"comment": "系统运行事件与异常日志，面向技术运维排查"},
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, comment="系统日志唯一编号"
    )
    level: Mapped[SystemLogLevel] = mapped_column(
        enum_column(SystemLogLevel, "level", 16),
        comment="日志级别：info/warning/error",
    )
    source: Mapped[str] = mapped_column(String(64), comment="产生日志的模块或组件")
    event: Mapped[str] = mapped_column(String(100), comment="稳定的技术事件编码")
    message: Mapped[str] = mapped_column(Text, comment="便于阅读的事件或异常摘要")
    request_id: Mapped[UUID | None] = mapped_column(
        Uuid, comment="关联后端请求 UUID；后台事件可为空，同一请求可重复"
    )
    details: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        default=dict,
        server_default=text("'{}'"),
        comment="已脱敏的结构化上下文，仅保存 JSON 对象",
    )
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=utc_now,
        server_default=func.now(),
        comment="事件发生时间，统一使用 UTC",
    )
