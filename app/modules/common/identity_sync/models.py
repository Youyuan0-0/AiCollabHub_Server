from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.modules.common.auth.models import UtcDateTime, utc_now


class IamSyncReceipt(Base):
    """已提交批次的去重凭据，不重复保存操作统计或失败日志。"""

    __tablename__ = "iam_sync_receipts"
    __table_args__ = (
        CheckConstraint("resource_type IN ('user', 'userlinkedrole')", name="resource_type"),
        CheckConstraint("action IN ('push', 'recycle')", name="action"),
        CheckConstraint("sync_time > 0", name="sync_time"),
        CheckConstraint("payload_hash ~ '^[0-9a-f]{64}$'", name="payload_hash").ddl_if(
            dialect="postgresql"
        ),
        Index("ix_iam_sync_receipts_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    payload_hash: Mapped[str] = mapped_column(String(64), unique=True)
    resource_type: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(16))
    sync_time: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utc_now, server_default=func.now()
    )
