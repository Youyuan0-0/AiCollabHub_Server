from __future__ import annotations

import hmac
import secrets
from datetime import timedelta
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlencode

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import SessionDependency
from app.db.transactions import commit_write, prepare_write
from app.modules.common.auth import service
from app.modules.common.auth.cas import IAMUnavailable, TicketRejected
from app.modules.common.auth.login_state import LoginState, consume_login_state, save_login_state
from app.modules.common.auth.models import (
    ActorType,
    AuthSession,
    OperationAction,
    UserRole,
    UserStatus,
)
from app.modules.common.auth.security import csrf_token, digest, safe_return_path
from app.modules.common.identity_sync.access import lock_identity

Scope = Literal["user", "admin"]
router = APIRouter(prefix="/auth", tags=["authentication"])
config_router = APIRouter(prefix="/auth", tags=["authentication"])


@config_router.get("/config")
def config(request: Request) -> dict:
    """返回前端初始化所需的认证启用状态与提供方标识。"""
    mode = request.app.state.settings.auth_mode
    return {"enabled": mode != "disabled", "provider": mode}


def set_cookie(response: Response, request: Request, name: str, value: str, ttl: int) -> None:
    """按统一安全属性写入业务认证 Cookie。"""
    response.set_cookie(
        name,
        value,
        max_age=ttl,
        httponly=True,
        secure=request.app.state.settings.auth_cookie_secure,
        samesite="lax",
        path="/api",
    )


def clear_login_cookie(response: Response, scope: str) -> None:
    """清除指定入口的一次性登录请求 Cookie。"""
    response.delete_cookie(service.login_cookie(scope), path="/api", httponly=True, samesite="lax")


def login_failure(
    request: Request, db: Session, scope: str, error: str, return_path: str
) -> RedirectResponse:
    """记录登录失败并跳转登录页，同时清除一次性登录 Cookie。"""
    db.rollback()
    service.system_event(request, "login", error)
    target = "/admin/login" if scope == "admin" else "/login"
    response = RedirectResponse(
        target + "?" + urlencode({"error": error, "redirect": return_path}), status_code=303
    )
    clear_login_cookie(response, scope)
    return response


@router.get("/login", response_model=None)
def login(
    request: Request,
    db: SessionDependency,
    scope: Scope = "user",
    return_to: Annotated[str | None, Query(max_length=2048)] = None,
    renew: bool = False,
) -> RedirectResponse:
    """保存并绑定一次性登录请求，跳转统一认证入口。"""
    settings = request.app.state.settings
    target = safe_return_path(return_to or ("/admin/" if scope == "admin" else "/"), scope)
    if not renew:
        try:
            service.authenticated_session(db, request, scope)
        except HTTPException as error:
            if error.detail == "account_unavailable":
                return login_failure(request, db, scope, "account_unavailable", target)
            if error.status_code == 403:
                return login_failure(request, db, scope, "admin_not_allowed", target)
            if error.status_code != 401:
                raise
        else:
            # 已登录时直接返回原页面；切换账号仍通过 renew=true 显式进入 IAM。
            return RedirectResponse(target, 303)
    state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    callback_origin = settings.auth_callback_origin or settings.auth_public_origin
    callback = str(callback_origin).rstrip("/") + "/api/v1/auth/callback?"
    callback += urlencode({"state": state, "scope": scope})
    save_login_state(
        request,
        digest(state),
        LoginState(
            browser_hash=digest(browser),
            scope=scope,
            service_url=callback,
            return_path=target,
        ),
    )
    response = RedirectResponse(request.app.state.cas_client.login_url(callback, renew), 303)
    set_cookie(response, request, service.login_cookie(scope), browser, settings.auth_login_ttl)
    return response


@router.get("/callback", response_model=None)
def callback(
    request: Request,
    db: SessionDependency,
    scope: Scope,
    state: Annotated[str, Query(min_length=20, max_length=200)],
    ticket: Annotated[str, Query(min_length=3, max_length=1024)],
) -> RedirectResponse:
    """原子消费登录请求、验证票据并创建业务会话。"""
    settings = request.app.state.settings
    pending = consume_login_state(
        request,
        digest(state),
        digest(request.cookies.get(service.login_cookie(scope), "")),
        scope,
    )
    default_path = "/admin/" if scope == "admin" else "/"
    if pending is None:
        return login_failure(request, db, scope, "login_request_expired", default_path)
    target, callback_url = pending.return_path, pending.service_url
    # Redis 已原子消费登录请求；外部 IAM 等待期间不占用数据库事务。
    try:
        principal = request.app.state.cas_client.validate(ticket, callback_url)
    except TicketRejected:
        return login_failure(request, db, scope, "invalid_ticket", target)
    except IAMUnavailable:
        return login_failure(request, db, scope, "iam_unavailable", target)
    prepare_write(db, settings)
    identity = lock_identity(db, principal.subject)
    # 正式 IAM 必须先收到可信身份推送，不能靠一次验票激活未知身份。
    if (
        settings.auth_mode == "mock"
        and identity.status == UserStatus.PENDING
        and identity.user_sync_time is None
    ):
        identity.status = UserStatus.ACTIVE
        identity.updated_at = service.now()
    if (
        settings.auth_mode == "mock"
        and identity.role_sync_time is None
        and principal.subject in settings.auth_mock_admin_subjects
    ):
        if identity.role != UserRole.ADMIN:
            identity.role, identity.updated_at = UserRole.ADMIN, service.now()
    if identity.user_sync_time is None and not identity.display_name:
        identity.display_name = principal.display_name
        identity.updated_at = service.now()
    if identity.status != UserStatus.ACTIVE:
        return login_failure(request, db, scope, "account_unavailable", target)
    if scope == "admin" and not service.can_access_admin(identity):
        return login_failure(request, db, scope, "admin_not_allowed", target)
    raw_token = secrets.token_urlsafe(32)
    try:
        with db.begin_nested():
            value = AuthSession(
                user_id=identity.id,
                token_hash=digest(raw_token),
                ticket_hash=digest(ticket),
                created_at=service.now(),
                expires_at=service.now() + timedelta(seconds=settings.auth_session_ttl),
            )
            db.add(value)
            db.flush()
    except IntegrityError:
        return login_failure(request, db, scope, "ticket_already_closed", target)
    service.revoke_browser_sessions(db, request)
    service.write_operation(
        db,
        action=OperationAction.LOGIN,
        actor_type=ActorType.USER,
        user_id=identity.id,
        target_id=value.id,
        request_id=request.state.request_id,
        data={"scope": scope},
    )
    commit_write(db)
    response = RedirectResponse(target, 303)
    set_cookie(response, request, service.cookie_name(), raw_token, settings.auth_session_ttl)
    for name in service.LEGACY_SESSION_COOKIES:
        response.delete_cookie(name, path="/api", httponly=True, samesite="lax")
    clear_login_cookie(response, scope)
    return response


@router.get("/session")
def current_session(request: Request, db: SessionDependency, scope: Scope = "user") -> JSONResponse:
    """返回共享会话的身份、有效期与 CSRF 令牌，保持前端身份编号契约。"""
    value, raw = service.authenticated_session(db, request, scope)
    response = JSONResponse(
        {
            "user": {
                "id": value.user.iam_subject,
                "displayName": value.user.display_name or value.user.iam_subject,
                "canAccessAdmin": service.can_access_admin(value.user),
            },
            "expiresAt": value.expires_at.isoformat(),
            "csrfToken": csrf_token(
                raw,
                (
                    request.app.state.settings.auth_mock_secret
                    if request.app.state.settings.auth_mode == "mock"
                    else request.app.state.settings.auth_session_secret
                ).get_secret_value(),
            ),
        }
    )
    return response


@router.post("/logout", response_model=None)
def logout(
    request: Request, db: SessionDependency, scope: Scope = "user", global_logout: bool = True
) -> JSONResponse:
    """校验 CSRF 后撤销本浏览器两端会话，返回本地或统一注销地址。"""
    settings = request.app.state.settings
    raw = service.session_tokens(request).get(service.cookie_name(), "")
    value = service.session_by_token(db, raw)
    service.verify_csrf(request, raw)
    prepare_write(db, settings)
    if (
        value is not None
        and value.user_id is not None
        and service.revoke_browser_sessions(db, request)
    ):
        service.write_operation(
            db,
            action=OperationAction.LOGOUT,
            actor_type=ActorType.USER,
            user_id=value.user_id,
            target_id=value.id,
            request_id=request.state.request_id,
            data={"scope": scope},
        )
    commit_write(db)
    target = str(settings.auth_public_origin).rstrip("/")
    target += "/admin/login?logged_out=1" if scope == "admin" else "/login?logged_out=1"
    response = JSONResponse(
        {
            "redirectUrl": request.app.state.cas_client.logout_url(target)
            if global_logout
            else target
        }
    )
    for name in (service.cookie_name(), *service.LEGACY_SESSION_COOKIES):
        response.delete_cookie(name, path="/api", httponly=True, samesite="lax")
    return response


@router.post("/single-logout", response_model=None)
async def single_logout(request: Request) -> dict:
    """校验统一注销通知来源与报文，并撤销对应票据的会话。"""
    settings = request.app.state.settings
    if settings.auth_mode == "mock" and not hmac.compare_digest(
        request.headers.get("x-mock-iam-key", "").encode(),
        settings.auth_mock_secret.get_secret_value().encode(),
    ):
        raise HTTPException(403, "notification_source_rejected")
    if request.headers.get("content-type", "").split(";")[0].lower() != (
        "application/x-www-form-urlencoded"
    ):
        raise HTTPException(415, "form_required")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 16384:
            raise HTTPException(413, "notification_too_large")
    if settings.auth_mode == "iam":
        await run_in_threadpool(
            request.app.state.iam_verifier.verify, "logout", bytes(body), request.headers
        )
    try:
        form = parse_qs(body.decode("utf-8"), strict_parsing=True, max_num_fields=2)
        if set(form) != {"logoutRequest"} or len(form["logoutRequest"]) != 1:
            raise ValueError()
        root = ElementTree.fromstring(form["logoutRequest"][0], forbid_dtd=True)
        namespace = "{urn:oasis:names:tc:SAML:2.0:protocol}"
        indices = root.findall(namespace + "SessionIndex")
        if root.tag != namespace + "LogoutRequest" or len(indices) != 1:
            raise ValueError()
        ticket = indices[0].text
        if not ticket or len(ticket) > 1024 or not ticket.startswith("ST-"):
            raise ValueError()
    except (ValueError, UnicodeError, ElementTree.ParseError, DefusedXmlException):
        raise HTTPException(400, "invalid_logout_notification") from None

    def persist() -> None:
        """在独立数据库会话中撤销统一注销通知指定的票据。"""
        with Session(request.app.state.database_engine) as db:
            service.revoke_ticket(db, request, ticket)

    await run_in_threadpool(persist)
    return {"ok": True}
