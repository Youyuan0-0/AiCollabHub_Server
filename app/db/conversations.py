"""会话和消息分别存表，PostgreSQL 使用 JSONB 保存工具、引用及附件元信息。"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# SQLite 用于隔离测试；生产目标方言采用 JSONB 和带时区时间戳。
json_type = JSON().with_variant(JSONB(), "postgresql")


class Conversation(Base):
    """会话摘要、所属用户及并发版本；删除保留空标记，防止迟到结果复活记录。"""

    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint("version >= 0", name="version_nonnegative"),
        Index("ix_conversations_history", "owner_id", "deleted", "updated_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    knowledge_base: Mapped[str] = mapped_column(Text, nullable=False)
    tools: Mapped[list] = mapped_column(json_type, nullable=False)
    owner_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class StoredConversationMessage(Base):
    """按会话内序号持久化消息；附件保留编号与展示信息，原文件继续由附件仓库管理。"""

    __tablename__ = "conversation_messages"
    __table_args__ = (
        CheckConstraint("position >= 0", name="position_nonnegative"),
        CheckConstraint("role IN ('user', 'assistant')", name="role_valid"),
    )

    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    id: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    knowledge_base: Mapped[str] = mapped_column(Text, nullable=False)
    tools: Mapped[list] = mapped_column(json_type, nullable=False)
    sources: Mapped[list] = mapped_column(json_type, nullable=False)
    attachments: Mapped[list] = mapped_column(json_type, nullable=False)
