from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import Base
from app.main import create_app
from app.modules.common.auth import router as auth_router
from app.modules.common.auth import service
from app.modules.common.auth.cas import IAMUnavailable, MockCasClient
from app.modules.common.auth.mock_iam import PREFIX, create_mock_iam
from app.modules.common.auth.models import AuthEventLog, AuthLoginRequest, AuthSession
from app.modules.common.auth.security import digest

SECRET = "test-only-secret-with-at-least-32-characters"


@pytest.fixture
def auth(tmp_path):
    settings = Settings(
        app_env="test",
        auth_mode="mock",
        auth_public_origin="http://testserver",
        auth_mock_iam_url="http://iam.test",
        auth_mock_secret=SECRET,
        database_url=f"sqlite:///{tmp_path / 'authentication.db'}",
    )
    engine = create_engine(settings.database_url.get_secret_value())
    Base.metadata.create_all(engine)
    cas = MockCasClient(settings)
    cas.client.close()
    with (
        TestClient(create_app(settings, cas_client=cas), follow_redirects=False) as a,
        TestClient(
            create_app(settings.model_copy(update={"instance_id": "B"}), cas_client=cas),
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
    assert a.get("/api/v1/admin/application-session").status_code == 401
    payload = {"data": {"name": "登录后的申请"}}
    assert a.post("/api/v1/application-publications", json=payload).status_code == 403
    record = a.post("/api/v1/application-publications", json=payload, headers=csrf(a)).json()
    assert record["createdBy"] == "mock-user" and record["organizationId"] == ""
    login(a, iam, scope="admin", subject="mock-admin")
    admin = a.get("/api/v1/admin/application-session").json()
    assert admin["current"]["id"] == "mock-admin" and admin["current"]["role"] == "admin"
    assert a.get("/api/v1/admin/application-dependencies").status_code == 200
    # IAM 切换身份会撤销旧会话；场景接口应要求重新登录，不能回退到演示发布者。
    assert a.get("/api/v1/application-session").status_code == 401


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
    assert a.get("/api/v1/auth/session").json()["user"]["displayName"] == "模拟普通用户"
    assert a.get("/api/v1/auth/session").json()["user"]["canAccessAdmin"] is False
    assert a.get("/api/v1/datasets").status_code == 200
    assert b.get("/api/v1/datasets").status_code == 200
    payload = {"type": "问题", "module": "模型", "title": "测试", "description": "测试内容"}
    assert a.post("/api/v1/feedback", json=payload).status_code == 403
    assert a.post("/api/v1/feedback", json=payload, headers=csrf(a)).status_code == 201
    assert b.get(callback).headers["location"].startswith("/login?error=login_request_expired")
    with Session(engine) as db:
        values = db.scalars(select(AuthSession)).all()
        assert len(values) == 1 and values[0].subject == "mock-user"
        assert values[0].token_hash != a.cookies.get("ach_session_user")
        assert values[0].ticket_hash != parse_qs(urlsplit(callback).query)["ticket"][0]
        assert db.scalar(select(AuthEventLog).where(AuthEventLog.result == "success")) is not None


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
    with Session(engine) as db:
        assert db.scalar(select(AuthLoginRequest)).consumed_at is None
        db.execute(update(AuthLoginRequest).values(expires_at=service.now() - 1))
        db.commit()
    assert "login_request_expired" in a.get(callback).headers["location"]
    assert a.get("/api/v1/auth/session").status_code == 401


def test_local_logout_and_expiry_are_shared(auth):
    a, b, iam, engine = auth
    login(a, iam)
    b.cookies.update(a.cookies)
    assert a.post("/api/v1/auth/logout").status_code == 403
    headers = csrf(a)
    response = a.post("/api/v1/auth/logout?global_logout=false", headers=headers)
    assert response.status_code == 200
    assert response.json()["redirectUrl"] == "http://testserver/login?logged_out=1"
    assert b.get("/api/v1/auth/session").status_code == 401
    login(a, iam)
    with Session(engine) as db:
        db.execute(update(AuthSession).values(expires_at=service.now() - 1))
        db.commit()
    assert a.get("/api/v1/datasets").status_code == 401


def test_admin_access_is_explicit_and_session_scope_cannot_be_forged(auth):
    a, b, iam, _ = auth
    response = login(a, iam, scope="admin")
    assert "admin_not_allowed" in response.headers["location"]
    assert a.get("/api/v1/admin/feedback").status_code == 401
    login(a, iam, subject="mock-admin")
    assert a.get("/api/v1/auth/session").json()["user"]["canAccessAdmin"] is True
    b.cookies.set("ach_session_admin", a.cookies.get("ach_session_user"), path="/api")
    assert b.get("/api/v1/admin/feedback").status_code == 401
    login(a, iam, subject="mock-admin", scope="admin")
    assert a.get("/api/v1/admin/feedback").status_code == 200


def test_single_logout_covers_both_frontends_and_is_idempotent(auth):
    a, b, iam, _ = auth
    login(a, iam, subject="mock-admin")
    # 已在 IAM 登录时，第二个应用无需重新选择账号。
    start = a.get("/api/v1/auth/login?scope=admin&return_to=/admin/feedback")
    issued = iam.get(start.headers["location"])
    assert issued.status_code == 303
    assert a.get(issued.headers["location"]).headers["location"] == "/admin/feedback"
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
        assert db.scalar(select(AuthSession).where(AuthSession.subject.is_not(None))) is None


def test_uncertain_commit_never_returns_a_session_cookie(auth, monkeypatch):
    a, _, iam, _ = auth
    callback = begin(a, iam)
    real_commit = auth_router.commit_write
    commits = 0

    def commit_then_lose_confirmation(db):
        nonlocal commits
        commits += 1
        real_commit(db)
        if commits == 2:
            raise HTTPException(503, "commit_outcome_unknown")

    monkeypatch.setattr(auth_router, "commit_write", commit_then_lose_confirmation)
    response = a.get(callback)
    assert response.status_code == 503
    assert "ach_session_user" not in a.cookies
    assert a.get("/api/v1/auth/session").status_code == 401


def test_switching_mock_account_revokes_previous_sessions(auth):
    a, b, iam, _ = auth
    login(a, iam)
    b.cookies.update(a.cookies)
    login(a, iam, subject="mock-admin", scope="admin")
    assert b.get("/api/v1/auth/session").status_code == 401
    assert a.get("/api/v1/auth/session?scope=admin").json()["user"]["id"] == "mock-admin"
