"""使用隔离数据库和文档形状的 HTTP 响应验证正式 IAM 适配与共享会话。"""

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from auth_resources import auth_resources_factory
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.core.config import Settings
from app.db.base import Base
from app.main import create_app
from app.modules.common.auth.cas import MockCasClient
from app.modules.common.auth.iam_cas import IamCasClient
from app.modules.common.auth.models import User

USER_PATH = "/api/v1/auth/iam/identity-sync"
IAM_ADDRESS = ("192.0.2.10", 50000)


def validate_ticket(request):
    """HTTP 边界返回与登录名不同的 workNo，验证同步和会话始终关联集团编号。"""
    assert request.url.path == "/authn-api/v5/cas/assigned/p3/serviceValidate"
    assert "x-mock-iam-key" not in request.headers
    if request.url.params["ticket"] == "ST-invalid":
        return httpx.Response(
            200, json={"serviceResponse": {"authenticationFailure": {"code": "INVALID_TICKET"}}}
        )
    return httpx.Response(
        200,
        json={
            "serviceResponse": {
                "authenticationSuccess": {
                    "user": "a-different-login-name",
                    "attributes": {"workNo": "group-001", "displayName": "集团用户"},
                }
            }
        },
    )


@pytest.fixture
def iam_app(tmp_path, request):
    """正式客户端只替换 HTTP 传输，身份接收及会话存储均使用真实实现。"""
    settings = Settings(
        _env_file=None,
        app_env="test",
        auth_mode="iam",
        auth_identity_sync_enabled=getattr(request, "param", True),
        database_url=f"sqlite:///{tmp_path / 'iam.db'}",
        auth_public_origin="http://testserver",
        auth_callback_origin="http://testserver:8086",
        auth_iam_origin="http://iam.test",
        auth_iam_app_id="assigned",
        auth_iam_admin_role_code="role-admin",
        auth_iam_sync_allowed_networks=["192.0.2.10/32"],
        auth_mock_admin_subjects=["group-001"],
        auth_session_secret="test-only-session-secret-at-least-32-characters",
    )
    engine = create_engine(settings.database_url.get_secret_value())
    Base.metadata.create_all(engine)
    cas = IamCasClient(settings, transport=httpx.MockTransport(validate_ticket))
    with TestClient(
        create_app(
            settings,
            cas_client=cas,
            resources_factory=auth_resources_factory(),
        ),
        follow_redirects=False,
        client=IAM_ADDRESS,
    ) as client:
        yield client, engine
    cas.close()
    engine.dispose()


def push(client: TestClient, *, role: bool = False, status: str = "2000", version: int = 100):
    """向正式入口提交文档字段，不发送 Mock 的 subject/roleCode 值。"""
    item = (
        {"userNo": "group-001", "roleCode": "role-admin", "status": "authorized"}
        if role
        else {"workNo": "group-001", "displayName": "集团用户", "status": status}
    )
    return client.post(
        USER_PATH,
        json={
            "appId": "assigned",
            "resType": "userlinkedrole" if role else "user",
            "action": "push",
            "syncTime": version,
            "size": "1",
            "data": [item],
        },
    )


def login(client: TestClient, *, scope: str = "user", ticket: str = "ST-test-001"):
    """同一站点不同端口走完整浏览器回调，且 Cookie 仍用于前端会话。"""
    result = client.get("/api/v1/auth/login", params={"scope": scope})
    assert result.status_code == 303
    service_url = parse_qs(urlsplit(result.headers["location"]).query)["service"][0]
    assert service_url.startswith("http://testserver:8086/api/v1/auth/callback?")
    return client.get(service_url + "&ticket=" + ticket)


def test_iam_mode_assembles_and_closes_real_client(tmp_path):
    """正式模式自动管理真实客户端生命周期，且不能注入 Mock 客户端。"""
    settings = Settings(
        _env_file=None,
        auth_mode="iam",
        database_url=f"sqlite:///{tmp_path / 'iam.db'}",
        auth_public_origin="http://testserver",
        auth_callback_origin="http://testserver:8086",
        auth_iam_origin="http://iam.test",
        auth_iam_app_id="assigned",
        auth_iam_admin_role_code="role-admin",
        auth_session_secret="test-only-session-secret-at-least-32-characters",
    )
    engine = create_engine(settings.database_url.get_secret_value())
    Base.metadata.create_all(engine)
    with TestClient(create_app(settings, resources_factory=auth_resources_factory())) as client:
        cas = client.app.state.cas_client
        assert isinstance(cas, IamCasClient)
        assert not cas.client.is_closed
        assert client.get("/api/v1/auth/config").json()["provider"] == "iam"
    assert cas.client.is_closed
    engine.dispose()
    with pytest.raises(ValueError, match="Mock CAS client cannot be used"):
        create_app(
            settings,
            cas_client=object.__new__(MockCasClient),
        )
    with pytest.raises(ValidationError, match="same host and scheme"):
        Settings(**{**settings.model_dump(), "auth_callback_origin": "http://other.test:8086"})


def test_production_iam_mode_protects_business_routes(tmp_path):
    """生产环境真实认证模式也必须先验证会话，健康检查仍可匿名访问。"""
    settings = Settings(
        _env_file=None,
        app_env="production",
        auth_mode="iam",
        database_url=f"sqlite:///{tmp_path / 'production.db'}",
        auth_public_origin="http://testserver",
        auth_callback_origin="http://testserver:8086",
        auth_iam_origin="http://iam.test",
        auth_iam_app_id="assigned",
        auth_iam_admin_role_code="role-admin",
        auth_session_secret="test-only-session-secret-at-least-32-characters",
    )
    engine = create_engine(settings.database_url.get_secret_value())
    Base.metadata.create_all(engine)
    with TestClient(
        create_app(
            settings,
            resources_factory=auth_resources_factory(),
        )
    ) as client:
        assert client.get("/api/v1/health/live").status_code == 200
        assert client.get("/api/v1/overview/home").status_code == 401
        assert client.post("/api/v1/auth/mock/identity-sync", json={}).status_code == 404
    engine.dispose()


def test_unsigned_push_then_shared_session_and_local_logout(iam_app):
    """未同步不激活；无签名身份授权同步后两端共享状态，本地退出同时失效。"""
    client, engine = iam_app
    assert client.get("/api/v1/auth/config").json() == {"enabled": True, "provider": "iam"}
    rejected = client.post(USER_PATH, json={})
    assert rejected.status_code == 422
    assert rejected.json() == {"resultCode": 422, "resultMsg": "sync_rejected"}
    assert client.post("/api/v1/auth/mock/identity-sync", json={}).status_code == 404
    assert "account_unavailable" in login(client).headers["location"]
    assert push(client).json() == {"resultCode": 200, "resultMsg": "success"}
    assert push(client).json() == {"resultCode": 200, "resultMsg": "success"}
    assert push(client, role=True, version=101).status_code == 200
    assert login(client, ticket="ST-test-002").headers["location"] == "http://testserver/"
    assert client.get("/api/v1/auth/session").json()["user"]["canAccessAdmin"] is True
    assert client.get("/api/v1/auth/session?scope=admin").status_code == 200
    with Session(engine) as db:
        user = db.scalar(select(User))
        assert user.iam_subject == "group-001" and user.role == "admin"
    assert client.post("/api/v1/auth/single-logout").status_code == 404
    session = client.get("/api/v1/auth/session").json()
    result = client.post(
        "/api/v1/auth/logout?global_logout=false",
        headers={"Origin": "http://testserver", "X-CSRF-Token": session["csrfToken"]},
    )
    assert result.status_code == 200
    assert result.json() == {"redirectUrl": "http://testserver/login?logged_out=1"}
    assert client.get("/api/v1/auth/session").status_code == 401
    assert client.get("/api/v1/auth/session?scope=admin").status_code == 401


def test_unknown_user_status_and_role_are_not_acknowledged(iam_app):
    """集团状态和角色未约定时不覆盖用户，也不返回成功回执。"""
    client, engine = iam_app
    invalid_status = push(client, status="9999")
    assert invalid_status.status_code == 422
    assert invalid_status.json() == {"resultCode": 422, "resultMsg": "sync_rejected"}
    invalid_role = {
        "appId": "assigned",
        "resType": "userlinkedrole",
        "action": "push",
        "syncTime": 100,
        "size": 1,
        "data": [{"userNo": "group-001", "roleCode": "other-role", "status": "authorized"}],
    }
    assert client.post(USER_PATH, json=invalid_role).status_code == 422
    with Session(engine) as db:
        assert db.scalar(select(User)) is None


@pytest.mark.parametrize(
    ("override", "expected_status"),
    [
        ({"appId": "wrong"}, 403),
        ({"resType": "org"}, 422),
        ({"size": 2}, 422),
        ({"data": [{"workNo": "group-001", "status": "bad"}]}, 422),
    ],
)
def test_unmapped_or_partial_push_is_rejected(iam_app, override, expected_status):
    """未确认的资源类型、状态和缺失条目都不能向 IAM 报成功。"""
    client, engine = iam_app
    payload = {
        "appId": "assigned",
        "resType": "user",
        "action": "push",
        "syncTime": 100,
        "size": 1,
        "data": [{"workNo": "group-001", "status": "2000"}],
        **override,
    }
    result = client.post(USER_PATH, json=payload)
    assert result.status_code == expected_status
    assert result.json() == {"resultCode": expected_status, "resultMsg": "sync_rejected"}
    with Session(engine) as db:
        assert db.scalar(select(User)) is None


def test_disabled_real_user_revokes_existing_shared_session(iam_app):
    """状态 3000 按停用处理，旧浏览器令牌不可再用。"""
    client, _ = iam_app
    assert push(client).status_code == 200
    assert login(client).status_code == 303
    assert push(client, status="3000", version=101).status_code == 200
    assert client.get("/api/v1/auth/session").status_code == 401


def test_untrusted_push_cannot_spoof_forwarded_headers(iam_app):
    """伪造代理头、Mock 密钥及 appId 不能绕过推送连接来源检查。"""
    client, engine = iam_app
    untrusted = TestClient(client.app, client=("198.51.100.8", 50000), follow_redirects=False)
    try:
        result = untrusted.post(
            USER_PATH,
            headers={
                "X-Forwarded-For": IAM_ADDRESS[0],
                "X-Real-IP": IAM_ADDRESS[0],
                "X-Mock-IAM-Key": "not-a-real-credential",
            },
            json={"appId": "assigned"},
        )
        assert result.status_code == 403
        assert result.json() == {"resultCode": 403, "resultMsg": "sync_rejected"}
    finally:
        untrusted.close()
    with Session(engine) as db:
        assert db.scalar(select(User)) is None


def test_empty_source_ranges_reject_push_before_database_writes(iam_app, monkeypatch):
    """未填写来源范围时，即使报文合法也不得创建或修改用户。"""
    client, engine = iam_app
    monkeypatch.setattr(client.app.state.settings, "auth_iam_sync_allowed_networks", [])
    assert push(client).status_code == 403
    with Session(engine) as db:
        assert db.scalar(select(User)) is None


def test_trusted_proxy_restores_actual_source_and_rejects_forged_chain(iam_app):
    """使用部署同款 Uvicorn 中间件，避免伪造链头 IP 获得推送权限。"""
    client, _ = iam_app
    proxy = TestClient(
        ProxyHeadersMiddleware(client.app, trusted_hosts="127.0.0.1"),
        client=("127.0.0.1", 50000),
    )
    try:
        proxy.headers["X-Forwarded-For"] = "192.0.2.10, 198.51.100.8"
        assert push(proxy).status_code == 403
        proxy.headers["X-Forwarded-For"] = "198.51.100.8, 192.0.2.10"
        assert push(proxy).status_code == 200
    finally:
        proxy.close()


def test_formal_user_does_not_inherit_mock_admin_list(iam_app):
    """同一编号即使在 Mock 管理员名单中，正式模式也必须等待角色推送。"""
    client, _ = iam_app
    assert push(client).status_code == 200
    login(client)
    assert client.get("/api/v1/auth/session").json()["user"]["canAccessAdmin"] is False
    assert client.get("/api/v1/auth/session?scope=admin").status_code == 403


@pytest.mark.parametrize("ticket", ["ST-test-001", "ST-invalid"])
def test_callback_failure_returns_to_public_origin(iam_app, ticket):
    """未同步账号及验票失败都返回网站登录页，不把浏览器留在 8086。"""
    client, _ = iam_app
    result = login(client, scope="admin", ticket=ticket)
    expected = "invalid_ticket" if ticket == "ST-invalid" else "account_unavailable"
    location = urlsplit(result.headers["location"])
    assert location.netloc == "testserver"
    assert location.path == "/admin/login"
    assert parse_qs(location.query) == {"error": [expected], "redirect": ["/admin/"]}


def test_existing_session_reuses_login_and_explicit_iam_logout(iam_app):
    """已登录切换管理端直接返回网站；显式统一退出生成真实 IAM 地址。"""
    client, _ = iam_app
    assert push(client).status_code == 200
    assert push(client, role=True).status_code == 200
    login(client)
    result = client.get(
        "/api/v1/auth/login", params={"scope": "admin", "return_to": "/admin/feedback?q=test"}
    )
    assert result.headers["location"] == "http://testserver/admin/feedback?q=test"
    session = client.get("/api/v1/auth/session").json()
    result = client.post(
        "/api/v1/auth/logout?global_logout=true&scope=admin",
        headers={"Origin": "http://testserver", "X-CSRF-Token": session["csrfToken"]},
    )
    target = urlsplit(result.json()["redirectUrl"])
    assert target.netloc == "iam.test"
    assert target.path == "/authn-api/v5/cas/assigned/logout"
    assert parse_qs(target.query)["service"] == ["http://testserver/admin/login?logged_out=1"]
    assert client.get("/api/v1/auth/session").status_code == 401


def test_revoke_admin_role_preserves_user_access(iam_app):
    """真实格式撤权成功后，旧会话仍能访问用户端，但不能继续使用管理权限。"""
    client, _ = iam_app
    assert push(client).status_code == 200
    assert push(client, role=True).status_code == 200
    login(client)
    assert client.get("/api/v1/auth/session?scope=admin").status_code == 200
    result = client.post(
        USER_PATH,
        json={
            "appId": "assigned",
            "resType": "userlinkedrole",
            "action": "push",
            "syncTime": 101,
            "size": 1,
            "data": [{"userNo": "group-001", "roleCode": "role-admin", "status": "unauthorized"}],
        },
    )
    assert result.json() == {"resultCode": 200, "resultMsg": "success"}
    assert client.get("/api/v1/auth/session").json()["user"]["canAccessAdmin"] is False
    assert client.get("/api/v1/auth/session?scope=admin").status_code == 403
