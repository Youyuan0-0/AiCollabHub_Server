from sqlalchemy import BigInteger, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuthSession(Base):
    __tablename__ = "auth_session"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    ticket_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scope: Mapped[str] = mapped_column(String(16))
    # 提前到达的登出通知也保留撤销标记，阻止随后完成验票的回调重新建立会话。
    subject: Mapped[str | None] = mapped_column(String(200))
    display_name: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[int] = mapped_column(BigInteger, index=True)
    revoked_at: Mapped[int | None] = mapped_column(BigInteger)


class AuthLoginRequest(Base):
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
    __tablename__ = "auth_event_log"
    __table_args__ = (Index("ix_auth_event_log_subject_created", "subject", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    subject: Mapped[str | None] = mapped_column(String(200))
    scope: Mapped[str] = mapped_column(String(16))
    event: Mapped[str] = mapped_column(String(40))
    result: Mapped[str] = mapped_column(String(40))
    request_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[int] = mapped_column(BigInteger, index=True)
