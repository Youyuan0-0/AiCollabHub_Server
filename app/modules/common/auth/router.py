import hmac
import secrets
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlencode

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import SessionDependency
from app.db.transactions import commit_write, prepare_write
from app.modules.common.auth import service
from app.modules.common.auth.cas import IAMUnavailable, TicketRejected
from app.modules.common.auth.models import AuthLoginRequest, AuthSession
from app.modules.common.auth.security import csrf_token, digest, safe_return_path

Scope = Literal["user", "admin"]
router = APIRouter(prefix="/auth", tags=["authentication"])
config_router = APIRouter(prefix="/auth", tags=["authentication"])


@config_router.get("/config")
def config(request: Request):
    return {"enabled": request.app.state.settings.auth_mode != "disabled", "provider": "mock"}


def set_cookie(response, request: Request, name: str, value: str, ttl: int):
    response.set_cookie(
        name,
        value,
        max_age=ttl,
        httponly=True,
        secure=request.app.state.settings.auth_cookie_secure,
        samesite="lax",
        path="/api",
    )


def clear_login_cookie(response, scope: str):
    response.delete_cookie(service.login_cookie(scope), path="/api", httponly=True, samesite="lax")


def login_failure(request: Request, db: Session, scope: str, error: str, return_path: str):
    prepare_write(db, request.app.state.settings)
    service.audit(db, request, scope, "login", error)
    commit_write(db)
    target = "/admin/login" if scope == "admin" else "/login"
    response = RedirectResponse(
        target + "?" + urlencode({"error": error, "redirect": return_path}), status_code=303
    )
    clear_login_cookie(response, scope)
    return response


@router.get("/login")
def login(
    request: Request,
    db: SessionDependency,
    scope: Scope = "user",
    return_to: Annotated[str | None, Query(max_length=2048)] = None,
    renew: bool = False,
):
    settings = request.app.state.settings
    target = safe_return_path(return_to or ("/admin/" if scope == "admin" else "/"), scope)
    state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    callback = str(settings.auth_public_origin).rstrip("/") + "/api/v1/auth/callback?"
    callback += urlencode({"state": state, "scope": scope})
    prepare_write(db, settings)
    db.add(
        AuthLoginRequest(
            state_hash=digest(state),
            browser_hash=digest(browser),
            scope=scope,
            service_url=callback,
            return_path=target,
            created_at=service.now(),
            expires_at=service.now() + settings.auth_login_ttl,
        )
    )
    commit_write(db)
    response = RedirectResponse(request.app.state.cas_client.login_url(callback, renew), 303)
    set_cookie(response, request, service.login_cookie(scope), browser, settings.auth_login_ttl)
    return response


@router.get("/callback")
def callback(
    request: Request,
    db: SessionDependency,
    scope: Scope,
    state: Annotated[str, Query(min_length=20, max_length=200)],
    ticket: Annotated[str, Query(min_length=3, max_length=1024)],
):
    settings = request.app.state.settings
    pending = db.get(AuthLoginRequest, digest(state))
    default_path = "/admin/" if scope == "admin" else "/"
    if (
        pending is None
        or pending.scope != scope
        or pending.expires_at <= service.now()
        or pending.consumed_at is not None
        or not hmac.compare_digest(
            pending.browser_hash, digest(request.cookies.get(service.login_cookie(scope), ""))
        )
    ):
        return login_failure(request, db, scope, "login_request_expired", default_path)
    target, callback_url = pending.return_path, pending.service_url
    prepare_write(db, settings)
    claimed = db.execute(
        update(AuthLoginRequest)
        .where(
            AuthLoginRequest.state_hash == digest(state),
            AuthLoginRequest.consumed_at.is_(None),
            AuthLoginRequest.expires_at > service.now(),
        )
        .values(consumed_at=service.now())
    )
    if claimed.rowcount != 1:
        return login_failure(request, db, scope, "login_request_expired", target)
    # 先原子消费登录请求，再调用外部 IAM；网络等待期间不占用数据库事务。
    commit_write(db)
    try:
        principal = request.app.state.cas_client.validate(ticket, callback_url)
    except TicketRejected:
        return login_failure(request, db, scope, "invalid_ticket", target)
    except IAMUnavailable:
        return login_failure(request, db, scope, "iam_unavailable", target)
    if scope == "admin" and not service.can_access_admin(settings, principal.subject):
        return login_failure(request, db, scope, "admin_not_allowed", target)
    raw_token = secrets.token_urlsafe(32)
    prepare_write(db, settings)
    try:
        with db.begin_nested():
            db.add(
                AuthSession(
                    token_hash=digest(raw_token),
                    ticket_hash=digest(ticket),
                    scope=scope,
                    subject=principal.subject,
                    display_name=principal.display_name,
                    created_at=service.now(),
                    expires_at=service.now() + settings.auth_session_ttl,
                )
            )
            db.flush()
    except IntegrityError:
        return login_failure(request, db, scope, "ticket_already_closed", target)
    previous = request.cookies.get(service.cookie_name(scope))
    if previous:
        db.execute(
            update(AuthSession)
            .where(AuthSession.token_hash == digest(previous))
            .values(
                revoked_at=service.now(),
            )
        )
    service.audit(db, request, scope, "login", "success", principal.subject)
    commit_write(db)
    response = RedirectResponse(target, 303)
    set_cookie(response, request, service.cookie_name(scope), raw_token, settings.auth_session_ttl)
    clear_login_cookie(response, scope)
    return response


@router.get("/session")
def current_session(request: Request, db: SessionDependency, scope: Scope = "user"):
    value = service.authenticated_session(db, request, scope)
    return {
        "user": {
            "id": value.subject,
            "displayName": value.display_name,
            "canAccessAdmin": service.can_access_admin(request.app.state.settings, value.subject),
        },
        "expiresAt": datetime.fromtimestamp(value.expires_at, UTC).isoformat(),
        "csrfToken": csrf_token(
            request.cookies[service.cookie_name(scope)],
            request.app.state.settings.auth_mock_secret.get_secret_value(),
        ),
    }


@router.post("/logout")
def logout(
    request: Request, db: SessionDependency, scope: Scope = "user", global_logout: bool = True
):
    service.verify_csrf(request, scope)
    settings = request.app.state.settings
    raw = request.cookies.get(service.cookie_name(scope), "")
    value = db.get(AuthSession, digest(raw))
    prepare_write(db, settings)
    if value is not None and value.scope == scope:
        value.revoked_at = service.now()
    service.audit(db, request, scope, "logout", "success", value.subject if value else None)
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
    response.delete_cookie(service.cookie_name(scope), path="/api", httponly=True, samesite="lax")
    return response


@router.post("/single-logout")
async def single_logout(request: Request):
    settings = request.app.state.settings
    if not hmac.compare_digest(
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

    def persist():
        with Session(request.app.state.database_engine) as db:
            service.revoke_ticket(db, request, ticket)

    await run_in_threadpool(persist)
    return {"ok": True}
