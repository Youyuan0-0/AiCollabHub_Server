import hmac
import secrets
import time
from uuid import uuid4

from fastapi import HTTPException, Request
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.transactions import commit_write, prepare_write
from app.modules.common.auth.models import AuthEventLog, AuthSession
from app.modules.common.auth.security import csrf_token, digest


def now() -> int:
    return int(time.time())


def cookie_name(scope: str) -> str:
    return "ach_session_" + scope


def login_cookie(scope: str) -> str:
    return "ach_login_" + scope


def can_access_admin(settings: Settings, subject: str | None) -> bool:
    # 用户端入口与管理端准入共用判断；真实 IAM 角色映射后在此替换模拟规则。
    return settings.auth_mode == "mock" and subject in settings.auth_mock_admin_subjects


def audit(
    db: Session, request: Request, scope: str, event: str, result: str, subject: str | None = None
) -> None:
    # 审计只存身份标识和结果码，不保存票据、Cookie、完整回调地址或个人档案。
    db.add(
        AuthEventLog(
            id=str(uuid4()),
            subject=subject,
            scope=scope,
            event=event,
            result=result,
            request_id=request.state.request_id,
            created_at=now(),
        )
    )


def authenticated_session(db: Session, request: Request, scope: str) -> AuthSession:
    raw = request.cookies.get(cookie_name(scope), "")
    value = db.get(AuthSession, digest(raw)) if 20 <= len(raw) <= 200 else None
    if (
        value is None
        or not value.subject
        or value.scope != scope
        or value.revoked_at is not None
        or value.expires_at <= now()
    ):
        raise HTTPException(401, "authentication_required")
    settings = request.app.state.settings
    if scope == "admin" and not can_access_admin(settings, value.subject):
        raise HTTPException(403, "admin_not_allowed")
    return value


def verify_csrf(request: Request, scope: str) -> None:
    settings = request.app.state.settings
    expected_origin = str(settings.auth_public_origin).rstrip("/")
    if request.headers.get("origin") != expected_origin:
        raise HTTPException(403, "csrf_rejected")
    expected = csrf_token(
        request.cookies.get(cookie_name(scope), ""),
        settings.auth_mock_secret.get_secret_value(),
    )
    if not hmac.compare_digest(expected.encode(), request.headers.get("x-csrf-token", "").encode()):
        raise HTTPException(403, "csrf_rejected")


def require_business_identity(request: Request) -> None:
    scope = "admin" if request.url.path.startswith("/api/v1/admin/") else "user"
    # 会话检查始终使用主库，不能因为备库或缓存延迟重新接受已撤销的会话。
    with Session(request.app.state.database_engine) as db:
        session = authenticated_session(db, request, scope)
        # 场景应用复用已验证身份，保存记录时不再另选一个演示用户。
        request.state.business_identity = {
            "id": session.subject,
            "name": session.display_name or session.subject,
            "scope": scope,
        }
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        verify_csrf(request, scope)


def revoke_ticket(db: Session, request: Request, ticket: str) -> None:
    settings: Settings = request.app.state.settings
    prepare_write(db, settings)
    ticket_hash = digest(ticket)
    values = dict(
        token_hash=digest(secrets.token_urlsafe(32)),
        ticket_hash=ticket_hash,
        scope="slo",
        subject=None,
        display_name=None,
        created_at=now(),
        expires_at=now() + settings.auth_session_ttl + settings.auth_login_ttl,
        revoked_at=now(),
    )
    # 唯一约束协调两个实例及回调/登出的竞态；找不到会话时也保留短期撤销标记。
    try:
        with db.begin_nested():
            db.execute(insert(AuthSession).values(**values))
    except IntegrityError:
        db.execute(
            update(AuthSession)
            .where(AuthSession.ticket_hash == ticket_hash)
            .values(
                revoked_at=now(),
            )
        )
    session = db.scalar(select(AuthSession).where(AuthSession.ticket_hash == ticket_hash))
    audit(db, request, session.scope, "single_logout", "success", session.subject)
    commit_write(db)
