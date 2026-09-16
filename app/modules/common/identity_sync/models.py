from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


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
