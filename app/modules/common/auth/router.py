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
    UserStatus,
)
from app.modules.common.auth.provisioning import provision_login_user
from app.modules.common.auth.security import csrf_token, digest, safe_return_path

Scope = Literal["user", "admin"]
router = APIRouter(prefix="/auth", tags=["authentication"])
config_router = APIRouter(prefix="/auth", tags=["authentication"])
single_logout_router = APIRouter(prefix="/auth", tags=["authentication"])


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


def frontend_url(request: Request, path: str) -> str:
    """独立端口回调返回配置的网站来源，避免停留在仅代理认证的 8086 端口。"""
    settings = request.app.state.settings
    if settings.auth_callback_origin:
        return str(settings.auth_public_origin).rstrip("/") + path
    return path


def login_failure(
    request: Request, db: Session, scope: str, error: str, return_path: str
) -> RedirectResponse:
    """记录登录失败并跳转登录页，同时清除一次性登录 Cookie。"""
    db.rollback()
    service.system_event(request, "login", error)
    target = "/admin/login" if scope == "admin" else "/login"
    target += "?" + urlencode({"error": error, "redirect": return_path})
    response = RedirectResponse(frontend_url(request, target), status_code=303)
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
            service.authentication_event(request, "login", "session_reused")
            return RedirectResponse(frontend_url(request, target), 303)
    state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    # 截短的随机状态摘要仅作日志关联，不能用于消费登录状态或建立会话。
    request.state.auth_flow_id = digest(state)[:24]
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
    service.authentication_event(
        request, "login", "redirect_to_iam", scope=scope, service_hash=digest(callback)[:24]
    )
    return response


@router.get("/home-callback", response_model=None)
def home_callback(request: Request) -> RedirectResponse:
    """将首页收到的票据转交原回调，不消费状态、不验票，也不改写保存的 service。"""
    values = {name: request.query_params.getlist(name) for name in ("ticket", "state", "scope")}
    has_unique_fields = all(len(items) == 1 for items in values.values())
    scope = "admin" if values["scope"] == ["admin"] else "user"
    state = values["state"][0] if len(values["state"]) == 1 else ""
    if 20 <= len(state) <= 200:
        request.state.auth_flow_id = digest(state)[:24]
    valid = (
        has_unique_fields
        and 3 <= len(values["ticket"][0]) <= 1024
        and 20 <= len(state) <= 200
        and values["scope"][0] in {"user", "admin"}
        and not any(
            ord(char) < 32 or ord(char) == 127
            for name in ("ticket", "state")
            for char in values[name][0]
        )
    )
    if not valid:
        service.authentication_event(
            request, "home_callback", "rejected", reason="invalid_callback_parameters", scope=scope
        )
        page = "/admin/login" if scope == "admin" else "/login"
        target = (
            frontend_url(request, page)
            + "?"
            + urlencode(
                {
                    "error": "invalid_callback_parameters",
                    "redirect": "/admin/" if scope == "admin" else "/",
                }
            )
        )
    else:
        service.authentication_event(request, "home_callback", "forwarded", scope=scope)
        # 浏览器先进入 /api 路径才能携带绑定 Cookie；忽略外部传入的 service 和返回地址。
        target = "/api/v1/auth/callback?" + urlencode(
            {name: items[0] for name, items in values.items()}
        )
    response = RedirectResponse(target, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
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
    request.state.auth_flow_id = digest(state)[:24]
    service.authentication_event(
        request,
        "callback",
        "received",
        scope=scope,
        binding_cookie_present=int(bool(request.cookies.get(service.login_cookie(scope)))),
    )
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
    service.authentication_event(
        request, "binding", "accepted", service_hash=digest(callback_url)[:24]
    )
    # Redis 已原子消费登录请求；外部 IAM 等待期间不占用数据库事务。
    try:
        principal = request.app.state.cas_client.validate(ticket, callback_url)
    except TicketRejected:
        return login_failure(request, db, scope, "invalid_ticket", target)
    except IAMUnavailable:
        return login_failure(request, db, scope, "iam_unavailable", target)
    prepare_write(db, settings)
    service.authentication_event(request, "identity", "started")
    identity = provision_login_user(db, principal, settings)
    service.authentication_event(request, "identity", "resolved")
    if identity.status != UserStatus.ACTIVE:
        return login_failure(request, db, scope, "account_unavailable", target)
    is_admin_denied = scope == "admin" and not service.can_access_admin(identity)
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
        data={"entryScope": scope} if is_admin_denied else {"scope": scope},
    )
    commit_write(db)
    service.authentication_event(
        request, "callback", "session_created", session_ref=value.id.hex, scope=scope
    )
    if is_admin_denied:
        # 身份认证成功仍建立普通用户会话，便于返回用户端和首次本地授权。
        service.system_event(request, "login", "admin_not_allowed")
        target = "/admin/login?" + urlencode({"error": "admin_not_allowed", "redirect": target})
    response = RedirectResponse(frontend_url(request, target), 303)
    set_cookie(response, request, service.cookie_name(), raw_token, settings.auth_session_ttl)
    for name in service.LEGACY_SESSION_COOKIES:
        response.delete_cookie(name, path="/api", httponly=True, samesite="lax")
    clear_login_cookie(response, scope)
    return response


@router.get("/session")
def current_session(request: Request, db: SessionDependency, scope: Scope = "user") -> JSONResponse:
    """返回共享会话的身份、有效期与 CSRF 令牌，保持前端身份编号契约。"""
    try:
        value, raw = service.authenticated_session(db, request, scope)
    except HTTPException as error:
        service.authentication_event(
            request,
            "session",
            "denied",
            scope=scope,
            status=error.status_code,
            reason=str(error.detail),
            session_cookie_present=int(bool(service.session_tokens(request))),
        )
        raise
    service.authentication_event(
        request, "session", "accepted", scope=scope, session_ref=value.id.hex
    )
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
    request: Request,
    db: SessionDependency,
    scope: Scope = "user",
    global_logout: bool = True,
) -> JSONResponse:
    """撤销两端共享会话并默认退出 IAM，避免自动登录立即恢复原账号。"""
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
    service.authentication_event(
        request,
        "logout",
        "local_session_revoked",
        scope=scope,
        session_ref=value.id.hex if value else "-",
        global_logout=int(global_logout),
    )
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


@single_logout_router.post("/single-logout", response_model=None)
async def single_logout(request: Request) -> dict:
    """仅供模拟 IAM 验证统一注销通知，正式集团暂不提供此能力。"""
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
