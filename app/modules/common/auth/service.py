import hmac
import logging
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.transactions import commit_write, prepare_write
from app.modules.common.auth.models import (
    ActorType,
    AuthSession,
    OperationAction,
    OperationLog,
    OperationTarget,
    SessionRecordType,
    UserStatus,
    utc_now,
)
from app.modules.common.auth.security import csrf_token, digest
from app.modules.common.identity_sync.access import can_access_admin, insert_missing

logger = logging.getLogger(__name__)
LEGACY_SESSION_COOKIES = ("ach_session_user", "ach_session_admin")


def now() -> datetime:
    """返回带时区时间，登录生命周期与数据库日期类型保持一致。"""
    return utc_now()


def cookie_name() -> str:
    """取得两端共用的会话 Cookie 名称。"""
    return "ach_session"


def login_cookie(scope: str) -> str:
    """取得指定入口的一次性登录状态 Cookie 名称。"""
    return "ach_login_" + scope


def session_tokens(request: Request) -> dict[str, str]:
    """仅接受共享会话 Cookie，旧入口 Cookie 不再具备独立身份语义。"""
    raw = request.cookies.get(cookie_name(), "")
    return {cookie_name(): raw} if 20 <= len(raw) <= 200 else {}


def write_operation(
    db: Session,
    *,
    action: OperationAction,
    actor_type: ActorType,
    user_id: UUID | None = None,
    target_type: OperationTarget = OperationTarget.SESSION,
    target_id: UUID | None = None,
    request_id: str | None = None,
    data: dict | None = None,
) -> None:
    """将成功操作加入当前事务；调用方提交业务后才对外确认成功。"""
    db.add(
        OperationLog(
            actor_type=actor_type,
            user_id=user_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id else None,
            request_id=UUID(request_id) if request_id else None,
            data=data or {},
        )
    )


def system_event(request: Request, action: str, result: str) -> None:
    """记录请求级认证诊断，不输出票据、Cookie 或供应方原始报文。"""
    logger.info("auth action=%s result=%s request_id=%s", action, result, request.state.request_id)


def session_by_token(db: Session, raw: str) -> AuthSession | None:
    """通过唯一令牌摘要查询会话，主键使用独立 UUID。"""
    return (
        db.scalar(select(AuthSession).where(AuthSession.token_hash == digest(raw))) if raw else None
    )


def authenticated_session(db: Session, request: Request, scope: str) -> tuple[AuthSession, str]:
    """校验共享会话与用户当前状态，权限始终从用户表读取。"""
    raw = session_tokens(request).get(cookie_name(), "")
    value = session_by_token(db, raw)
    if value is None or value.record_type != SessionRecordType.SESSION or value.user is None:
        raise HTTPException(401, "authentication_required")
    if value.user.status != UserStatus.ACTIVE:
        raise HTTPException(401, "account_unavailable")
    if value.revoked_at is not None or value.expires_at <= now():
        raise HTTPException(401, "authentication_required")
    if scope == "admin" and not can_access_admin(value.user):
        raise HTTPException(403, "admin_not_allowed")
    return value, raw


def verify_csrf(request: Request, raw_token: str) -> None:
    """同时校验来源和共享会话令牌对应的 CSRF 值。"""
    settings = request.app.state.settings
    expected_origin = str(settings.auth_public_origin).rstrip("/")
    expected = csrf_token(raw_token, settings.auth_mock_secret.get_secret_value())
    if (
        not raw_token
        or request.headers.get("origin") != expected_origin
        or not hmac.compare_digest(
            expected.encode(), request.headers.get("x-csrf-token", "").encode()
        )
    ):
        system_event(request, "csrf", "rejected")
        raise HTTPException(403, "csrf_rejected")


def revoke_browser_sessions(db: Session, request: Request) -> bool:
    """撤销共享会话并返回是否实际变更，避免并发退出重复写成功日志。"""
    hashes = [digest(raw) for raw in session_tokens(request).values()]
    if hashes:
        return (
            db.execute(
                update(AuthSession)
                .where(AuthSession.token_hash.in_(hashes), AuthSession.revoked_at.is_(None))
                .values(revoked_at=now())
                .returning(AuthSession.id)
            ).first()
            is not None
        )
    return False


def require_business_identity(request: Request) -> None:
    """注入已验证业务身份；保留现有业务 subject 契约，内部会话关联 UUID。"""
    scope = "admin" if request.url.path.startswith("/api/v1/admin/") else "user"
    with Session(request.app.state.database_engine) as db:
        session, raw = authenticated_session(db, request, scope)
        request.state.business_identity = {
            "id": session.user.iam_subject,
            "name": session.user.display_name or session.user.iam_subject,
            "scope": scope,
        }
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        verify_csrf(request, raw)


def revoke_ticket(db: Session, request: Request, ticket: str) -> None:
    """原子创建提前撤销标记或撤销现有会话，重复通知不重复写成功日志。"""
    settings = request.app.state.settings
    prepare_write(db, settings)
    current = now()
    ticket_hash = digest(ticket)
    inserted = insert_missing(
        db,
        AuthSession,
        dict(
            ticket_hash=ticket_hash,
            record_type=SessionRecordType.TICKET_REVOCATION,
            user_id=None,
            token_hash=None,
            created_at=current,
            expires_at=current
            + timedelta(seconds=settings.auth_session_ttl + settings.auth_login_ttl),
            revoked_at=current,
        ),
        conflict_columns=["ticket_hash"],
    )
    changed = inserted
    if not inserted:
        changed = (
            db.execute(
                update(AuthSession)
                .where(AuthSession.ticket_hash == ticket_hash, AuthSession.revoked_at.is_(None))
                .values(revoked_at=current)
                .returning(AuthSession.id)
            ).first()
            is not None
        )
    value = db.scalar(select(AuthSession).where(AuthSession.ticket_hash == ticket_hash))
    if changed:
        write_operation(
            db,
            action=OperationAction.SINGLE_LOGOUT,
            actor_type=ActorType.IAM,
            target_id=value.id,
            request_id=request.state.request_id,
        )
    commit_write(db)
