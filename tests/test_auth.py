import re
import time
from datetime import timedelta
from html import unescape
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from auth_resources import auth_resources_factory
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import Base
from app.main import create_app
from app.modules.common.auth import login_state, service
from app.modules.common.auth import router as auth_router
from app.modules.common.auth.cas import IAMUnavailable, MockCasClient
from app.modules.common.auth.mock_iam import PREFIX, create_mock_iam
from app.modules.common.auth.models import AuthSession, OperationLog, User, UserRole
from app.modules.common.auth.security import csrf_token, digest

SECRET = "test-only-secret-with-at-least-32-characters"


@pytest.fixture
def auth(tmp_path):
    settings = Settings(
        app_env="test",
        auth_mode="mock",
        auth_identity_sync_enabled=True,
        auth_public_origin="http://testserver",
        auth_mock_iam_url="http://iam.test",
        auth_mock_secret=SECRET,
        database_url=f"sqlite:///{tmp_path / 'authentication.db'}",
    )
    engine = create_engine(settings.database_url.get_secret_value())
    Base.metadata.create_all(engine)
    resources = auth_resources_factory()
    cas = MockCasClient(settings)
    cas.client.close()
    with (
        TestClient(
            create_app(settings, cas_client=cas, resources_factory=resources),
            follow_redirects=False,
        ) as a,
        TestClient(
            create_app(
                settings.model_copy(update={"instance_id": "B"}),
                cas_client=cas,
                resources_factory=resources,
            ),
            follow_redirects=False,
        ) as b,
        TestClient(
            create_mock_iam(settings, notification_client=b),
            base_url="http://iam.test",
            headers={"X-Mock-IAM-Key": SECRET},
            follow_redirects=False,
        ) as iam,
    ):
        cas.client = iam
        yield a, b, iam, engine
    engine.dispose()


def begin(a, iam, scope="user", subject="mock-user", return_to=None):
    response = a.get(
        "/api/v1/auth/login",
        params={
            "scope": scope,
            "return_to": return_to or ("/admin/feedback" if scope == "admin" else "/datasets"),
            "renew": True,
        },
    )
    assert response.status_code == 303
    location = response.headers["location"]
    service_url = parse_qs(urlsplit(location).query)["service"][0]
    assert iam.get(location).status_code == 200
    issued = iam.post(
        PREFIX + "/login",
        data={
            "service": service_url,
            "challenge": iam.cookies.get("ach_mock_form"),
            "subject": subject,
        },
        headers={"Origin": "http://iam.test"},
    )
    assert issued.status_code == 303
    return issued.headers["location"]


def login(a, iam, **kwargs):
    response = a.get(begin(a, iam, **kwargs))
    assert response.status_code == 303
    return response


def csrf(a, scope="user"):
    session = a.get("/api/v1/auth/session", params={"scope": scope})
    assert session.status_code == 200
    return {"Origin": "http://testserver", "X-CSRF-Token": session.json()["csrfToken"]}


def notification(ticket):
    return {
        "logoutRequest": (
            '<samlp:LogoutRequest xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol">'
            f"<samlp:SessionIndex>{ticket}</samlp:SessionIndex></samlp:LogoutRequest>"
        )
    }


@pytest.mark.parametrize(
    ("subject", "display_name", "is_admin"),
    [
        ("mock-user", "模拟用户A", False),
        ("mock-user-b", "模拟用户B", False),
        ("mock-user-c", "模拟用户C", False),
        ("mock-admin", "模拟管理员A", True),
        ("mock-admin-b", "模拟管理员B", True),
    ],
)
def test_mock_accounts_login_with_distinct_identities_and_correct_access(
    auth, subject, display_name, is_admin
):
    """五个账号的身份、姓名和管理准入必须由实际登录结果决定。"""
    a, b, iam, engine = auth
    login(a, iam, subject=subject)
    b.cookies.update(a.cookies)
    for client in (a, b):
        current = client.get("/api/v1/auth/session").json()["user"]
        assert current == {
            "id": subject,
            "displayName": display_name,
            "canAccessAdmin": is_admin,
        }
        assert client.get("/api/v1/admin/model-configuration").status_code == (
            200 if is_admin else 403
        )
    with Session(engine) as db:
        session = service.session_by_token(db, a.cookies.get("ach_session"))
        assert session.user.iam_subject == subject
        assert session.user.role == ("admin" if is_admin else "user")


def test_new_mock_users_keep_independent_browser_sessions(auth):
    """不同浏览器登录 B、C 后，退出 B 不得注销 C 或串用用户身份。"""
    a, b, iam, engine = auth
    login(a, iam, subject="mock-user-b")
    # 清空模拟 IAM 浏览器 Cookie，表示第二个独立浏览器，保留服务端 B 会话。
    iam.cookies.clear()
    login(b, iam, subject="mock-user-c")
    assert a.get("/api/v1/auth/session").json()["user"]["id"] == "mock-user-b"
    assert b.get("/api/v1/auth/session").json()["user"]["id"] == "mock-user-c"
    with Session(engine) as db:
        rows = db.scalars(select(User)).all()
        assert {row.iam_subject for row in rows} == {"mock-user-b", "mock-user-c"}
        assert len({row.id for row in rows}) == 2
    assert a.post("/api/v1/auth/logout?global_logout=false", headers=csrf(a)).status_code == 200
    assert a.get("/api/v1/auth/session").status_code == 401
    assert b.get("/api/v1/auth/session").json()["user"]["id"] == "mock-user-c"


def test_applications_use_verified_identity_and_disable_demo_switch(auth):
    a, _, iam, _ = auth
    login(a, iam)
    actor = a.get("/api/v1/application-session").json()
    assert actor["current"]["id"] == "mock-user"
    assert actor["current"]["organizationId"] == ""
    assert actor["actors"] == [] and actor["directory"]["organizations"] == []
    assert (
        a.post(
            "/api/v1/application-demo-session",
            json={"actorId": "demo-admin", "audience": "user"},
            headers=csrf(a),
        ).status_code
        == 404
    )
    assert a.get("/api/v1/admin/application-session").status_code == 403
    payload = {"data": {"name": "登录后的申请"}}
    assert a.post("/api/v1/application-publications", json=payload).status_code == 403
    record = a.post("/api/v1/application-publications", json=payload, headers=csrf(a)).json()
    assert record["createdBy"] == "mock-user" and record["organizationId"] == ""
    login(a, iam, scope="admin", subject="mock-admin")
    is_admin = a.get("/api/v1/admin/application-session").json()
    assert is_admin["current"]["id"] == "mock-admin" and is_admin["current"]["role"] == "admin"
    assert a.get("/api/v1/admin/application-dependencies").status_code == 200
    # 切换账号后两端统一使用新身份，不能回退到原用户或演示发布者。
    assert a.get("/api/v1/application-session").json()["current"]["id"] == "mock-admin"


def test_cross_instance_callback_session_and_business_csrf(auth):
    a, b, iam, engine = auth
    assert a.get("/api/v1/auth/config").json()["enabled"] is True
    assert a.get("/api/v1/datasets").status_code == 401
    callback = begin(a, iam, return_to="/datasets?q=test")
    b.cookies.update(a.cookies)
    response = b.get(callback)
    assert response.headers["location"] == "/datasets?q=test"
    assert "HttpOnly" in response.headers["set-cookie"]
    a.cookies.update(b.cookies)
    assert a.get("/api/v1/auth/session").json()["user"]["displayName"] == "模拟用户A"
    assert a.get("/api/v1/auth/session").json()["user"]["canAccessAdmin"] is False
    assert a.get("/api/v1/datasets").status_code == 200
    assert b.get("/api/v1/datasets").status_code == 200
    payload = {
        "type": "function_error",
        "module": "models",
        "title": "测试",
        "description": "测试内容",
        "contact": "1234",
        "clientRequestId": "3c614bbe-466b-4fb2-8d57-d0b33b5c8cb4",
    }
    assert a.post("/api/v1/feedback", json=payload).status_code == 403
    # 会话和 CSRF 已通过；本测试的 SQLite 身份库不提供 PostgreSQL 反馈表。
    assert a.post("/api/v1/feedback", json=payload, headers=csrf(a)).status_code == 503
    assert b.get(callback).headers["location"].startswith("/login?error=login_request_expired")
    with Session(engine) as db:
        values = db.scalars(select(AuthSession)).all()
        assert len(values) == 1 and values[0].user.iam_subject == "mock-user"
        assert values[0].token_hash == digest(a.cookies.get("ach_session"))
        assert values[0].record_type == "session"
        assert values[0].ticket_hash != parse_qs(urlsplit(callback).query)["ticket"][0]
        assert db.scalar(select(OperationLog).where(OperationLog.action == "login")) is not None


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/",
        "//evil.example/",
        "/\\evil.example/",
        "/%2f%2fevil.example/",
        "/%0d%0aLocation:evil",
        "/admin/services",
        "/api/v1/auth/logout",
        "/login",
    ],
)
def test_login_rejects_unsafe_redirects(auth, target):
    a, _, _, _ = auth
    assert a.get("/api/v1/auth/login", params={"return_to": target}).status_code == 400


def test_callback_requires_browser_binding_and_live_state(auth):
    a, b, iam, engine = auth
    callback = begin(a, iam)
    assert "login_request_expired" in b.get(callback).headers["location"]
    cache = a.app.state.resources.cache
    state = parse_qs(urlsplit(callback).query)["state"][0]
    key = f"{cache.namespace}:auth:login:{digest(state)}"
    assert cache.client.exists(key) == 1
    assert cache.client.ttl(key) > 0
    # 删除表示 TTL 到期；错误浏览器先到不能消费状态，缺失状态必须重新登录。
    cache.client.delete(key)
    assert "login_request_expired" in a.get(callback).headers["location"]
    assert a.get("/api/v1/auth/session").status_code == 401


@pytest.mark.parametrize("scope", ["user", "admin"])
def test_local_logout_and_expiry_are_shared(auth, scope):
    a, b, iam, engine = auth
    login(a, iam, subject="mock-admin", scope=scope)
    b.cookies.update(a.cookies)
    assert a.post("/api/v1/auth/logout").status_code == 403
    headers = csrf(a)
    response = a.post(f"/api/v1/auth/logout?global_logout=false&scope={scope}", headers=headers)
    assert response.status_code == 200
    prefix = "/admin" if scope == "admin" else ""
    assert response.json()["redirectUrl"] == f"http://testserver{prefix}/login?logged_out=1"
    assert "ach_session" not in a.cookies
    assert b.get("/api/v1/auth/session").status_code == 401
    assert b.get("/api/v1/auth/session?scope=admin").status_code == 401
    login(a, iam, subject="mock-admin", scope=scope)
    with Session(engine) as db:
        db.execute(
            update(AuthSession).values(
                created_at=service.now() - timedelta(hours=1),
                expires_at=service.now() - timedelta(seconds=1),
            )
        )
        db.commit()
    assert a.get("/api/v1/datasets").status_code == 401
    assert a.get("/api/v1/admin/feedback").status_code == 401


def test_admin_access_is_explicit_and_session_scope_cannot_be_forged(auth):
    a, b, iam, _ = auth
    response = login(a, iam, scope="admin")
    assert "admin_not_allowed" in response.headers["location"]
    assert a.get("/api/v1/admin/feedback").status_code == 403
    assert a.get("/api/v1/auth/session").json()["user"]["id"] == "mock-user"
    login(a, iam)
    assert a.get("/api/v1/admin/feedback").status_code == 403
    assert a.get("/api/v1/auth/session?scope=admin").json()["detail"] == "admin_not_allowed"
    response = a.get("/api/v1/auth/login?scope=admin&return_to=/admin/feedback")
    assert "admin_not_allowed" in response.headers["location"]
    assert a.get("/api/v1/auth/session").json()["user"]["id"] == "mock-user"
    b.cookies.set("ach_session_admin", a.cookies.get("ach_session"), path="/api")
    assert b.get("/api/v1/admin/feedback").status_code == 401
    b.cookies.update(a.cookies)
    assert b.get("/api/v1/admin/feedback").status_code == 403


def test_single_logout_covers_both_frontends_and_is_idempotent(auth):
    a, b, iam, _ = auth
    login(a, iam, subject="mock-admin")
    # 切换前端直接复用平台会话，不再创建第二次 CAS 登录。
    start = a.get("/api/v1/auth/login?scope=admin&return_to=/admin/feedback")
    assert start.headers["location"] == "/admin/feedback"
    b.cookies.update(a.cookies)
    response = a.post("/api/v1/auth/logout", headers=csrf(a))
    assert iam.get(response.json()["redirectUrl"]).status_code == 303
    assert b.get("/api/v1/auth/session").status_code == 401
    assert b.get("/api/v1/auth/session?scope=admin").status_code == 401


def test_logout_arriving_before_callback_prevents_session_creation(auth):
    a, b, iam, engine = auth
    callback = begin(a, iam)
    ticket = parse_qs(urlsplit(callback).query)["ticket"][0]
    for _ in range(2):
        assert (
            b.post(
                "/api/v1/auth/single-logout",
                data=notification(ticket),
                headers={"X-Mock-IAM-Key": SECRET},
            ).status_code
            == 200
        )
    assert "ticket_already_closed" in a.get(callback).headers["location"]
    assert a.get("/api/v1/auth/session").status_code == 401
    with Session(engine) as db:
        assert db.scalar(
            select(AuthSession).where(AuthSession.ticket_hash == digest(ticket))
        ).revoked_at


def test_notification_authentication_xml_and_size_limits(auth):
    a, _, _, _ = auth
    assert a.post("/api/v1/auth/single-logout", data=notification("ST-test")).status_code == 403
    headers = {"X-Mock-IAM-Key": SECRET}
    assert (
        a.post(
            "/api/v1/auth/single-logout", json=notification("ST-test"), headers=headers
        ).status_code
        == 415
    )
    xml = '<!DOCTYPE root [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><root>&xxe;</root>'
    assert (
        a.post(
            "/api/v1/auth/single-logout", data={"logoutRequest": xml}, headers=headers
        ).status_code
        == 400
    )
    assert (
        a.post(
            "/api/v1/auth/single-logout", data={"logoutRequest": "x" * 17000}, headers=headers
        ).status_code
        == 413
    )


def test_simulator_validates_service_and_consumes_tickets_once(auth):
    a, _, iam, _ = auth
    assert iam.get(PREFIX + "/login?service=https://evil.example/").status_code == 400
    callback = begin(a, iam)
    fields = parse_qs(urlsplit(callback).query)
    ticket = fields.pop("ticket")[0]
    service_url = "http://testserver/api/v1/auth/callback?" + urlencode(
        {key: value[0] for key, value in fields.items()}
    )
    params = {"ticket": ticket, "service": service_url}
    assert (
        "authenticationSuccess"
        in iam.get(PREFIX + "/p3/serviceValidate", params=params).json()["serviceResponse"]
    )
    assert (
        "authenticationFailure"
        in iam.get(PREFIX + "/p3/serviceValidate", params=params).json()["serviceResponse"]
    )


@pytest.mark.parametrize("scope", ["user", "admin"])
@pytest.mark.parametrize("invalidated", ["missing_cookie", "replaced_cookie", "expired"])
def test_stale_iam_form_offers_fresh_login_without_authenticating(
    auth, monkeypatch, scope, invalidated
):
    """旧表单、丢失 Cookie 和过期请求须拒绝认证，并允许用户重新发起完整登录。"""
    a, _, iam, _ = auth
    start = a.get("/api/v1/auth/login", params={"scope": scope})
    location = start.headers["location"]
    service_url = parse_qs(urlsplit(location).query)["service"][0]
    assert iam.get(location).status_code == 200
    challenge = iam.cookies.get("ach_mock_form")
    if invalidated == "missing_cookie":
        iam.cookies.delete("ach_mock_form")
    elif invalidated == "replaced_cookie":
        # 同一浏览器另开认证页面会覆盖 Cookie，旧页面的隐藏令牌仍为原值。
        assert iam.get(location).status_code == 200
        assert iam.cookies.get("ach_mock_form") != challenge
    with monkeypatch.context() as clock:
        if invalidated == "expired":
            current = time.time()
            clock.setattr(time, "time", lambda: current + 301)
        rejected = iam.post(
            PREFIX + "/login",
            data={"service": service_url, "subject": "mock-admin", "challenge": challenge},
            headers={"Origin": "http://iam.test"},
        )
    assert rejected.status_code == 403
    assert "text/html" in rejected.headers["content-type"]
    assert "重新登录" in rejected.text
    assert "ach_mock_iam" not in iam.cookies
    assert a.get("/api/v1/auth/session").status_code == 401
    retry = unescape(re.search(r'href="([^"]+)"', rejected.text)[1])
    assert retry == f"http://testserver/api/v1/auth/login?scope={scope}&renew=true"
    # 恢复入口必须重新建立平台浏览器绑定，不能沿用旧回调或自动提交旧表单。
    fresh = a.get(retry)
    fresh_location = fresh.headers["location"]
    assert iam.get(fresh_location).status_code == 200
    issued = iam.post(
        PREFIX + "/login",
        data={
            "service": parse_qs(urlsplit(fresh_location).query)["service"][0],
            "subject": "mock-admin",
            "challenge": iam.cookies.get("ach_mock_form"),
        },
        headers={"Origin": "http://iam.test"},
    )
    assert issued.status_code == 303
    assert a.get(issued.headers["location"]).status_code == 303
    assert a.get("/api/v1/auth/session").json()["user"]["id"] == "mock-admin"


def test_mock_authentication_cannot_be_enabled_in_production():
    with pytest.raises(ValidationError, match="prohibited"):
        Settings(
            app_env="production",
            database_url="sqlite://",
            auth_mode="mock",
            auth_mock_secret=SECRET,
        )
    with pytest.raises(ValidationError, match="32 characters"):
        Settings(database_url="sqlite://", auth_mode="mock", auth_mock_secret="short")


def test_ticket_expiry_and_provider_failure_do_not_establish_sessions(auth, monkeypatch):
    a, _, iam, engine = auth
    callback = begin(a, iam)
    import time

    current = time.time()
    with monkeypatch.context() as clock:
        clock.setattr(time, "time", lambda: current + 61)
        response = a.get(callback)
        assert "invalid_ticket" in response.headers["location"]
    callback = begin(a, iam)

    def unavailable(*args):
        raise IAMUnavailable()

    monkeypatch.setattr(a.app.state.cas_client, "validate", unavailable)
    assert "iam_unavailable" in a.get(callback).headers["location"]
    with Session(engine) as db:
        assert db.scalar(select(AuthSession).where(AuthSession.user_id.is_not(None))) is None


def test_uncertain_commit_never_returns_a_session_cookie(auth, monkeypatch):
    a, _, iam, _ = auth
    callback = begin(a, iam)
    real_commit = auth_router.commit_write
    commits = 0

    def commit_then_lose_confirmation(db):
        nonlocal commits
        commits += 1
        real_commit(db)
        if commits == 1:
            raise HTTPException(503, "commit_outcome_unknown")

    monkeypatch.setattr(auth_router, "commit_write", commit_then_lose_confirmation)
    response = a.get(callback)
    assert response.status_code == 503
    assert "ach_session" not in a.cookies
    assert a.get("/api/v1/auth/session").status_code == 401


def test_switching_mock_account_revokes_previous_sessions(auth):
    a, b, iam, _ = auth
    login(a, iam)
    b.cookies.update(a.cookies)
    login(a, iam, subject="mock-admin", scope="admin")
    assert b.get("/api/v1/auth/session").status_code == 401
    assert a.get("/api/v1/auth/session?scope=admin").json()["user"]["id"] == "mock-admin"
    assert a.get("/api/v1/auth/session").json()["user"]["id"] == "mock-admin"


@pytest.mark.parametrize("entry_scope", ["user", "admin"])
def test_one_login_covers_both_frontends_and_instances(auth, entry_scope):
    a, b, iam, engine = auth
    login(a, iam, subject="mock-admin", scope=entry_scope)
    raw = a.cookies.get("ach_session")
    b.cookies.update(a.cookies)
    tokens = set()
    for client in (a, b):
        for scope, target in (("user", "/datasets?q=AI"), ("admin", "/admin/services")):
            value = client.get(f"/api/v1/auth/session?scope={scope}")
            assert value.status_code == 200
            assert value.json()["user"]["id"] == "mock-admin"
            tokens.add(value.json()["csrfToken"])
            response = client.get(
                "/api/v1/auth/login", params={"scope": scope, "return_to": target}
            )
            assert response.status_code == 303 and response.headers["location"] == target
            assert client.cookies.get("ach_session") == raw
        assert client.get("/api/v1/datasets").status_code == 200
        assert client.get("/api/v1/admin/model-configuration").status_code == 200
    assert len(tokens) == 1
    with Session(engine) as db:
        assert len(db.scalars(select(AuthSession)).all()) == 1
    assert a.app.state.resources.cache.client.dbsize() == 0
    # 显式切换账号仍创建新的登录请求。
    response = a.get("/api/v1/auth/login?scope=admin&renew=true")
    assert response.headers["location"].startswith("http://iam.test/")


@pytest.mark.parametrize("old_cookie", ["ach_session_user", "ach_session_admin"])
def test_old_entry_cookies_are_not_valid_login_credentials(auth, old_cookie):
    a, b, iam, _ = auth
    login(a, iam, subject="mock-admin")
    b.cookies.set(old_cookie, a.cookies.get("ach_session"), domain="testserver.local", path="/api")
    for scope in ("user", "admin"):
        assert b.get(f"/api/v1/auth/session?scope={scope}").status_code == 401
    login(b, iam)
    assert old_cookie not in b.cookies
    assert b.get("/api/v1/auth/session").json()["user"]["id"] == "mock-user"


@pytest.mark.parametrize("shared_state", ["expired", "revoked", "missing", "malformed"])
def test_invalid_shared_session_never_falls_back_to_legacy(auth, shared_state):
    a, _, iam, engine = auth
    login(a, iam, subject="mock-admin")
    raw = a.cookies.get("ach_session")
    a.cookies.set("ach_session_admin", raw, path="/api")
    with Session(engine) as db:
        value = service.session_by_token(db, raw)
        if shared_state == "expired":
            value.created_at = service.now() - timedelta(hours=1)
            value.expires_at = service.now() - timedelta(seconds=1)
        elif shared_state == "revoked":
            value.revoked_at = service.now()
        elif shared_state == "missing":
            db.delete(value)
        else:
            a.cookies.delete("ach_session")
            a.cookies.set("ach_session", "bad", path="/api")
        db.commit()
    for scope in ("user", "admin"):
        assert a.get(f"/api/v1/auth/session?scope={scope}").status_code == 401


def test_shared_session_keeps_csrf_and_current_admin_checks(auth):
    a, _, iam, engine = auth
    login(a, iam, subject="mock-admin")
    headers = csrf(a)
    for invalid in (
        {**headers, "Origin": "http://evil.test"},
        {**headers, "X-CSRF-Token": "wrong"},
    ):
        assert a.post("/api/v1/auth/logout?scope=admin", headers=invalid).status_code == 403
    assert a.get("/api/v1/auth/session?scope=admin").status_code == 200
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.iam_subject == "mock-admin"))
        user.role, user.role_sync_time = UserRole.USER, 1
        db.commit()
    assert a.get("/api/v1/auth/session?scope=admin").status_code == 403
    assert a.get("/api/v1/auth/session").json()["user"]["canAccessAdmin"] is False
    login(a, iam, subject="mock-user")
    assert a.post("/api/v1/auth/logout", headers=headers).status_code == 403
    assert csrf(a)["X-CSRF-Token"] == csrf_token(a.cookies.get("ach_session"), SECRET)


def test_wrong_browser_cannot_consume_another_browsers_state(auth):
    a, b, iam, _ = auth
    callback = begin(a, iam)
    assert "login_request_expired" in b.get(callback).headers["location"]
    assert a.get(callback).headers["location"] == "/datasets"
    assert a.get("/api/v1/auth/session").status_code == 200


def test_redis_failure_prevents_new_login_but_keeps_existing_session(auth, monkeypatch):
    a, b, iam, _ = auth
    login(a, iam)
    iam.cookies.clear()
    callback = begin(b, iam)
    client = a.app.state.resources.cache.client

    def unavailable(*args, **kwargs):
        raise RedisConnectionError("test failure")

    monkeypatch.setattr(client, "set", unavailable)
    monkeypatch.setattr(client, "eval", unavailable)
    assert b.get("/api/v1/auth/login?renew=true").status_code == 503
    assert b.get(callback).status_code == 503
    assert b.get("/api/v1/auth/session").status_code == 401
    assert a.get("/api/v1/auth/session").status_code == 200
    assert a.get("/api/v1/auth/login?return_to=/models").headers["location"] == "/models"


def test_admin_denial_is_logged_without_discarding_valid_user_login(auth, caplog):
    a, _, iam, engine = auth
    with caplog.at_level("INFO", logger=service.__name__):
        response = login(a, iam, scope="admin")
    assert "admin_not_allowed" in response.headers["location"]
    assert "admin_not_allowed" in caplog.text
    with Session(engine) as db:
        operation = db.scalar(select(OperationLog))
        assert operation.action == "login"
        assert operation.data == {"entryScope": "admin"}
        assert db.scalar(select(AuthSession)) is not None


def test_mock_admin_bootstrap_does_not_override_later_local_revocation(auth):
    """Mock 名单仅负责首次建号，已有账号的本地撤权不会在重新登录时被覆盖。"""
    a, _, iam, engine = auth
    login(a, iam, subject="mock-admin")
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.iam_subject == "mock-admin"))
        user.role = UserRole.USER
        db.commit()
    login(a, iam, subject="mock-admin")
    assert a.get("/api/v1/auth/session").json()["user"]["canAccessAdmin"] is False


def test_redis_state_consumption_is_atomic(auth):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    a, _, iam, _ = auth
    callback = begin(a, iam)
    state = parse_qs(urlsplit(callback).query)["state"][0]
    cache = a.app.state.resources.cache
    key = f"{cache.namespace}:auth:login:{digest(state)}"
    browser = digest(a.cookies.get("ach_login_user"))
    barrier = Barrier(2)

    def consume():
        barrier.wait(timeout=5)
        return cache.client.eval(login_state.CONSUME_LOGIN, 1, key, browser, "user")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: consume(), range(2)))
    assert sum(value is not None for value in results) == 1
