"""独立开发服务：单进程运行的模拟 IAM，不提供生产认证能力。"""

import hmac
import secrets
import time
from dataclasses import dataclass, field
from html import escape
from threading import Lock
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.config import Settings

PREFIX = "/authn-api/v5/cas/platform"
ACCOUNTS = {"mock-user": "模拟普通用户", "mock-admin": "模拟管理员"}


@dataclass
class IAMSession:
    subject: str
    expires: float
    tickets: list[tuple[str, str]] = field(default_factory=list)
    closing: bool = False


@dataclass
class Ticket:
    service: str
    subject: str
    expires: float
    used: bool = False


def create_mock_iam(settings: Settings | None = None, notification_client=None) -> FastAPI:
    settings = settings or Settings()
    if settings.app_env == "production" or settings.auth_mode != "mock":
        raise RuntimeError("The simulator requires AUTH_MODE=mock in development/test")
    app = FastAPI(title="模拟 IAM", docs_url=None, redoc_url=None, openapi_url=None)
    tickets: dict[str, Ticket] = {}
    sessions: dict[str, IAMSession] = {}
    challenges: dict[str, float] = {}
    lock = Lock()
    origin = str(settings.auth_public_origin).rstrip("/")
    iam_origin = str(settings.auth_mock_iam_url).rstrip("/")
    secret = settings.auth_mock_secret.get_secret_value()

    @app.middleware("http")
    async def private_response(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        # 同源表单保留 Origin，跨源回跳不发送 Referer 中的登录上下文。
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    def check_service(value: str, callback: bool = True):
        parsed = urlsplit(value)
        expected = {"/api/v1/auth/callback"} if callback else {"/login", "/admin/login"}
        if (
            len(value) > 4096
            or f"{parsed.scheme}://{parsed.netloc}" != origin
            or parsed.path not in expected
            or parsed.fragment
            or "\\" in value
            or any(ord(char) < 32 for char in value)
        ):
            raise HTTPException(400, "unregistered_service")

    def collect():
        current = time.time()
        for mapping in (tickets, sessions, challenges):
            for key, item in list(mapping.items()):
                expires = item if isinstance(item, float) else item.expires
                if expires <= current:
                    del mapping[key]
            if len(mapping) >= 10000:
                raise HTTPException(429, "simulator_capacity_reached")

    def issue(value: str, session_key: str):
        ticket = "ST-" + secrets.token_urlsafe(32)
        identity = sessions[session_key]
        tickets[ticket] = Ticket(value, identity.subject, time.time() + 60)
        identity.tickets.append((ticket, value))
        return RedirectResponse(value + "&" + urlencode({"ticket": ticket}), 303)

    @app.get(PREFIX + "/login")
    def login(request: Request, service: str, renew: bool = False):
        check_service(service)
        with lock:
            collect()
            session_key = request.cookies.get("ach_mock_iam", "")
            if not renew and session_key in sessions and not sessions[session_key].closing:
                return issue(service, session_key)
            challenge = secrets.token_urlsafe(32)
            challenges[challenge] = time.time() + 300
        html = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>模拟统一认证</title><style>
        *{{box-sizing:border-box}}body{{margin:0;background:#f4f6f8;color:#202c3b;
        font:16px/1.6 system-ui,'Microsoft YaHei',sans-serif}}main{{max-width:440px;
        margin:12vh auto;padding:28px}}h1{{font-size:24px;margin:0 0 8px}}p{{color:#536176}}
        form{{display:grid;gap:18px}}label{{display:flex;gap:10px;align-items:center;
        padding:12px;border:1px solid #c6cfd9;border-radius:6px;background:white}}
        button{{padding:12px;background:#236be8;color:white;border:0;border-radius:6px;
        font:inherit;cursor:pointer}}small{{color:#667085}}</style>
        <main><h1>模拟统一认证</h1><p>开发测试账号</p>
        <form method="post" action="{PREFIX}/login">
        <input type="hidden" name="service" value="{escape(service, quote=True)}">
        <input type="hidden" name="challenge" value="{challenge}">
        <label><input type="radio" name="subject" value="mock-user" checked>模拟普通用户</label>
        <label><input type="radio" name="subject" value="mock-admin">模拟管理员</label>
        <button type="submit">登录</button></form><p><small>此服务仅用于平台联调。</small></p>
        </main></html>"""
        response = HTMLResponse(html)
        response.set_cookie(
            "ach_mock_form",
            challenge,
            max_age=300,
            httponly=True,
            samesite="lax",
            path=PREFIX,
            secure=iam_origin.startswith("https:"),
        )
        return response

    @app.post(PREFIX + "/login")
    async def login_submit(request: Request):
        if request.headers.get("origin") != iam_origin:
            raise HTTPException(403, "invalid_origin")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                raise HTTPException(413)
        try:
            form = parse_qs(body.decode(), strict_parsing=True, max_num_fields=3)
            if set(form) != {"service", "subject", "challenge"} or any(
                len(value) != 1 for value in form.values()
            ):
                raise ValueError()
            service, subject, challenge = (
                form[key][0] for key in ("service", "subject", "challenge")
            )
        except (UnicodeError, ValueError):
            raise HTTPException(400, "invalid_login_form") from None
        check_service(service)
        if subject not in ACCOUNTS or not hmac.compare_digest(
            challenge.encode(), request.cookies.get("ach_mock_form", "").encode()
        ):
            raise HTTPException(403, "invalid_login_form")
        # 切换模拟账号前撤销旧 IAM 会话，避免两个前端残留不同账号的登录状态。
        if not await run_in_threadpool(close_session, request.cookies.get("ach_mock_iam", "")):
            raise HTTPException(503, "previous_session_logout_incomplete")
        with lock:
            collect()
            if challenge not in challenges:
                raise HTTPException(403, "expired_login_form")
            del challenges[challenge]
            session_key = secrets.token_urlsafe(32)
            sessions[session_key] = IAMSession(subject, time.time() + settings.auth_session_ttl)
            response = issue(service, session_key)
        response.set_cookie(
            "ach_mock_iam",
            session_key,
            max_age=settings.auth_session_ttl,
            httponly=True,
            samesite="lax",
            path=PREFIX,
            secure=iam_origin.startswith("https:"),
        )
        response.delete_cookie("ach_mock_form", path=PREFIX)
        return response

    @app.get(PREFIX + "/p3/serviceValidate")
    def validate(request: Request, service: str, ticket: str, format: str = "json"):
        if not hmac.compare_digest(
            request.headers.get("x-mock-iam-key", "").encode(), secret.encode()
        ):
            raise HTTPException(403, "mock_key_required")
        check_service(service)
        with lock:
            collect()
            value = tickets.get(ticket)
            if value is None or value.used or value.service != service:
                return {"serviceResponse": {"authenticationFailure": {"code": "INVALID_TICKET"}}}
            value.used = True
            return {
                "serviceResponse": {
                    "authenticationSuccess": {
                        "user": value.subject,
                        "attributes": {"displayName": ACCOUNTS[value.subject]},
                    }
                }
            }

    def close_session(session_key: str) -> bool:
        with lock:
            identity = sessions.get(session_key)
            if identity:
                identity.closing = True
            notifications = list(identity.tickets) if identity else []
            for ticket, _ in notifications:
                if ticket in tickets:
                    tickets[ticket].used = True
        failed = False
        client = notification_client or httpx.Client(timeout=5, trust_env=False)
        try:
            for ticket, _ in notifications:
                xml = (
                    '<samlp:LogoutRequest xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol">'
                    f"<samlp:SessionIndex>{ticket}</samlp:SessionIndex></samlp:LogoutRequest>"
                )
                try:
                    response = client.post(
                        origin + "/api/v1/auth/single-logout",
                        data={"logoutRequest": xml},
                        headers={"X-Mock-IAM-Key": secret},
                    )
                    if response.status_code != 200:
                        failed = True
                except httpx.HTTPError:
                    failed = True
        finally:
            if notification_client is None:
                client.close()
        if failed:
            return False
        with lock:
            sessions.pop(session_key, None)
        return True

    @app.get(PREFIX + "/logout")
    def logout(request: Request, service: str):
        check_service(service, callback=False)
        if not close_session(request.cookies.get("ach_mock_iam", "")):
            return HTMLResponse(
                '<html lang="zh-CN"><meta charset="utf-8"><p>部分应用退出未完成，'
                "请稍后刷新重试。</p></html>",
                status_code=503,
            )
        response = RedirectResponse(service, 303)
        response.delete_cookie("ach_mock_iam", path=PREFIX)
        return response

    return app
