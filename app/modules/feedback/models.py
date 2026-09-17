"""反馈业务共用三张表，附件正文由 MinIO 保存。"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FeedbackRecord(Base):
    """反馈正文及当前处理版本，提交人与请求编号共同保证幂等。"""

    __tablename__ = "feedback_records"
    __table_args__ = (
        UniqueConstraint("submitter_id", "client_request_id", name="uq_feedback_submission"),
        CheckConstraint("status IN ('processing', 'resolved')", name="feedback_status"),
        CheckConstraint("version >= 1", name="feedback_version"),
        CheckConstraint("length(trim(contact)) BETWEEN 1 AND 200", name="feedback_contact"),
        CheckConstraint("length(trim(title)) BETWEEN 1 AND 60", name="feedback_title"),
        CheckConstraint(
            "length(trim(description)) BETWEEN 1 AND 5000", name="feedback_description"
        ),
        CheckConstraint(
            "(status = 'processing' AND resolved_at IS NULL) OR "
            "(status = 'resolved' AND resolved_at IS NOT NULL)",
            name="feedback_resolution",
        ),
        Index("ix_feedback_owner_time", "submitter_id", "created_at", "id"),
        Index("ix_feedback_status_time", "status", "created_at", "id"),
        Index("ix_feedback_time", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    submitter_id: Mapped[str] = mapped_column(String(200))
    submitter_name: Mapped[str] = mapped_column(String(100))
    submitter_org_id: Mapped[str | None] = mapped_column(String(200))
    feedback_type: Mapped[str] = mapped_column(String(32))
    type_other: Mapped[str | None] = mapped_column(String(200))
    module_code: Mapped[str] = mapped_column(String(32))
    module_other: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(60))
    description: Mapped[str] = mapped_column(Text)
    contact: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="processing")
    version: Mapped[int] = mapped_column(default=1)
    client_request_id: Mapped[str] = mapped_column(String(36))
    payload_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FeedbackAttachment(Base):
    """上传暂存及反馈附件元信息，清理只能领取尚未绑定的附件。"""

    __tablename__ = "feedback_attachments"
    __table_args__ = (
        UniqueConstraint("uploader_id", "client_request_id", name="uq_feedback_upload"),
        UniqueConstraint("storage_backend", "object_key", name="uq_feedback_object"),
        CheckConstraint("byte_size >= 0", name="feedback_attachment_size"),
        CheckConstraint(
            "state IN ('uploading', 'staged', 'attached', 'deleting')",
            name="feedback_attachment_state",
        ),
        CheckConstraint(
            "(state = 'attached' AND feedback_id IS NOT NULL AND expires_at IS NULL) OR "
            "(state IN ('uploading', 'staged', 'deleting') AND feedback_id IS NULL "
            "AND expires_at IS NOT NULL)",
            name="feedback_attachment_binding",
        ),
        CheckConstraint(
            "state NOT IN ('staged', 'attached') OR (byte_size > 0 AND sha256 IS NOT NULL)",
            name="feedback_attachment_ready",
        ),
        Index("ix_feedback_attachment_record", "feedback_id", "created_at", "id"),
        Index("ix_feedback_attachment_cleanup", "state", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    feedback_id: Mapped[str | None] = mapped_column(
        ForeignKey("feedback_records.id", ondelete="RESTRICT")
    )
    uploader_id: Mapped[str] = mapped_column(String(200))
    client_request_id: Mapped[str] = mapped_column(String(36))
    original_name: Mapped[str] = mapped_column(String(200))
    content_type: Mapped[str] = mapped_column(String(128))
    byte_size: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64))
    storage_backend: Mapped[str] = mapped_column(String(20), default="minio")
    storage_bucket: Mapped[str] = mapped_column(String(255))
    object_key: Mapped[str] = mapped_column(String(512))
    security_level: Mapped[str | None] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(20), default="uploading")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FeedbackEvent(Base):
    """提交、回复与状态变更的追加记录，与主记录在同一事务内维护。"""

    __tablename__ = "feedback_events"
    __table_args__ = (
        UniqueConstraint(
            "feedback_id", "actor_id", "client_request_id", name="uq_feedback_event_retry"
        ),
        UniqueConstraint("feedback_id", "resulting_version", name="uq_feedback_event_version"),
        CheckConstraint("resulting_version >= 1", name="feedback_event_version"),
        CheckConstraint(
            "(action = 'submitted' AND from_status IS NULL AND to_status = 'processing' "
            "AND resulting_version = 1 AND content = '') OR "
            "(action = 'replied' AND from_status IS NOT NULL "
            "AND from_status IN ('processing', 'resolved') AND to_status = from_status "
            "AND resulting_version > 1) OR "
            "(action = 'resolved' AND from_status = 'processing' AND from_status IS NOT NULL "
            "AND to_status = 'resolved' AND resulting_version > 1) OR "
            "(action = 'reopened' AND from_status = 'resolved' AND from_status IS NOT NULL "
            "AND to_status = 'processing' AND resulting_version > 1)",
            name="feedback_event_transition",
        ),
        CheckConstraint(
            "action = 'submitted' OR length(trim(content)) BETWEEN 1 AND 5000",
            name="feedback_event_content",
        ),
        Index("ix_feedback_event_time", "feedback_id", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    feedback_id: Mapped[str] = mapped_column(ForeignKey("feedback_records.id", ondelete="RESTRICT"))
    actor_id: Mapped[str] = mapped_column(String(200))
    actor_name: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(20))
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    resulting_version: Mapped[int]
    client_request_id: Mapped[str] = mapped_column(String(36))
    payload_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
