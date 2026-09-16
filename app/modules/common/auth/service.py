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
from app.modules.common.identity_sync.access import can_access_admin, is_identity_enabled

LEGACY_SESSION_COOKIES = {"ach_session_user": "user", "ach_session_admin": "admin"}


def now() -> int:
    """返回 Unix 时间戳，认证有效期统一按秒计算。"""
    return int(time.time())


def cookie_name() -> str:
    """取得用户端和管理端共用的会话 Cookie 名称。"""
    return "ach_session"


def session_tokens(request: Request) -> dict[str, str]:
    """读取长度有效的共享及旧入口令牌，供会话校验和统一撤销使用。"""
    return {
        name: raw
        for name in (cookie_name(), *LEGACY_SESSION_COOKIES)
        if 20 <= len(raw := request.cookies.get(name, "")) <= 200
    }


def login_cookie(scope: str) -> str:
    """取得指定入口的临时登录状态 Cookie 名称。"""
    return "ach_login_" + scope


def audit(
    db: Session, request: Request, scope: str, event: str, result: str, subject: str | None = None
) -> None:
    """将认证审计加入当前事务，只记录身份标识和结果码，不保存敏感凭据。"""
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


def authenticated_session(db: Session, request: Request, scope: str) -> tuple[AuthSession, str]:
    """校验共享会话、身份状态和入口权限，返回有效会话与实际使用的令牌。"""
    # 新 Cookie 一旦存在就具有唯一优先级；失效后不能通过旧 Cookie 恢复登录。
    tokens = session_tokens(request)
    if cookie_name() in request.cookies:
        tokens = {cookie_name(): tokens[cookie_name()]} if cookie_name() in tokens else {}
    matches = []
    is_blocked = False
    for name, raw in tokens.items():
        value = db.get(AuthSession, digest(raw))
        allowed_scopes = (
            {"platform", "user", "admin"}
            if name == cookie_name()
            else {LEGACY_SESSION_COOKIES[name]}
        )
        if (
            value is not None
            and value.subject
            and value.scope in allowed_scopes
            and value.expires_at > now()
        ):
            if not is_identity_enabled(db, value.subject):
                is_blocked = True
            elif value.revoked_at is None:
                matches.append((value, raw))
    # 升级前两端若残留不同账号，不自动选择其中的管理员身份。
    if not matches or len({value.subject for value, _ in matches}) != 1:
        raise HTTPException(401, "account_unavailable" if is_blocked else "authentication_required")
    value, raw = max(matches, key=lambda item: (item[0].created_at, item[0].token_hash))
    settings = request.app.state.settings
    # scope 表示本次访问的入口，管理员准入由服务端身份判断，不能由 Cookie 名决定。
    if scope == "admin" and not can_access_admin(db, settings, value.subject):
        raise HTTPException(403, "admin_not_allowed")
    return value, raw


def verify_csrf(request: Request, raw_token: str) -> None:
    """同时校验请求来源和实际会话令牌对应的 CSRF 值，失败返回 403。"""
    settings = request.app.state.settings
    expected_origin = str(settings.auth_public_origin).rstrip("/")
    if not raw_token or request.headers.get("origin") != expected_origin:
        raise HTTPException(403, "csrf_rejected")
    expected = csrf_token(
        raw_token,
        settings.auth_mock_secret.get_secret_value(),
    )
    if not hmac.compare_digest(expected.encode(), request.headers.get("x-csrf-token", "").encode()):
        raise HTTPException(403, "csrf_rejected")


def revoke_browser_sessions(db: Session, request: Request) -> None:
    """在当前事务中撤销本浏览器携带的共享和旧入口会话。"""
    # 同时撤销本浏览器携带的旧会话，避免退出或切换账号后遗留另一端的有效令牌。
    hashes = [digest(raw) for raw in session_tokens(request).values()]
    if hashes:
        db.execute(
            update(AuthSession).where(AuthSession.token_hash.in_(hashes)).values(revoked_at=now())
        )


def require_business_identity(request: Request) -> None:
    """为业务请求注入已验证身份，并对写入请求执行 CSRF 校验。"""
    scope = "admin" if request.url.path.startswith("/api/v1/admin/") else "user"
    # 所有 worker 查询同一数据库，撤销后不能从进程内缓存恢复旧会话。
    with Session(request.app.state.database_engine) as db:
        session, raw = authenticated_session(db, request, scope)
        # 场景应用复用已验证身份，保存记录时不再另选一个演示用户。
        request.state.business_identity = {
            "id": session.subject,
            "name": session.display_name or session.subject,
            "scope": scope,
        }
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        verify_csrf(request, raw)


def revoke_ticket(db: Session, request: Request, ticket: str) -> None:
    """撤销票据对应会话并提交审计；会话尚未建立时保留短期撤销标记。"""
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
    # 唯一约束协调多个 worker 及回调/登出的竞态；找不到会话时也保留短期撤销标记。
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
