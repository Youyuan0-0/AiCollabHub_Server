"""使用隔离的假可信边界验证正式模式编排，不模拟集团的实际签名算法。"""

from collections.abc import Mapping
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from auth_resources import auth_resources_factory
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import Base
from app.main import create_app
from app.modules.common.auth.cas import MockCasClient, Principal
from app.modules.common.auth.models import User

PROOF = "test-only-verified-source"
USER_PATH = "/api/v1/auth/iam/identity-sync"


class VerifiedCas:
    """仅用于测试：返回同一 workNo，绝不替代生产 CAS 验票签名。"""

    def login_url(self, service: str, renew: bool = False) -> str:
        """保留传给 CAS 的完整 service 地址以验证入口端口。"""
        return "http://iam.test/authn-api/v5/cas/assigned/login?" + urlencode({"service": service})

    def logout_url(self, service: str) -> str:
        """注销后只返回平台登录页。"""
        return "http://iam.test/authn-api/v5/cas/assigned/logout?" + urlencode({"service": service})

    def validate(self, ticket: str, service: str) -> Principal:
        """假设外部校验已通过，返回与用户推送相同的稳定编号。"""
        return Principal("group-001", "集团用户")

    def close(self) -> None:
        """测试客户端没有持久连接。"""


class VerifiedIngress:
    """仅供测试注入，不能作为生产来源校验实现。"""

    def verify(self, purpose: str, body: bytes, headers: Mapping[str, str]) -> None:
        """测试错误来源必须在处理报文前被拒绝。"""
        if headers.get("x-test-proof") != PROOF:
            raise HTTPException(403, "notification_source_rejected")


@pytest.fixture
def iam_app(tmp_path):
    """使用独立 SQLite、Redis 模拟器和显式假边界验证所有会话状态。"""
    settings = Settings(
        _env_file=None,
        app_env="test",
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
    with TestClient(
        create_app(
            settings,
            cas_client=VerifiedCas(),
            iam_verifier=VerifiedIngress(),
            resources_factory=auth_resources_factory(),
        ),
        follow_redirects=False,
    ) as client:
        yield client, engine
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
        headers={"X-Test-Proof": PROOF},
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
    callback = urlsplit(service_url)
    return client.get(callback.path + "?" + callback.query + "&ticket=" + ticket)


def test_iam_mode_fails_closed_without_verified_implementations(tmp_path):
    """配置文件不能凭模式开关绕过尚未提供的集团签名与来源认证。"""
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
    with pytest.raises(ValueError, match="verified CAS client and notification verifier"):
        create_app(settings)
    with pytest.raises(ValueError, match="Mock CAS client cannot be used"):
        create_app(
            settings,
            cas_client=object.__new__(MockCasClient),
            iam_verifier=VerifiedIngress(),
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
            cas_client=VerifiedCas(),
            iam_verifier=VerifiedIngress(),
            resources_factory=auth_resources_factory(),
        )
    ) as client:
        assert client.get("/api/v1/health/live").status_code == 200
        assert client.get("/api/v1/overview/home").status_code == 401
        assert client.post("/api/v1/auth/mock/identity-sync", json={}).status_code == 404
    engine.dispose()


def test_real_shaped_push_then_shared_session_and_single_logout(iam_app):
    """未同步不激活；正式用户及授权同步后两端共享状态并响应登出。"""
    client, engine = iam_app
    assert client.get("/api/v1/auth/config").json() == {"enabled": True, "provider": "iam"}
    rejected = client.post(USER_PATH, json={})
    assert rejected.status_code == 403
    assert rejected.json() == {"resultCode": 403, "resultMsg": "sync_rejected"}
    assert client.post("/api/v1/auth/mock/identity-sync", json={}).status_code == 404
    assert "account_unavailable" in login(client).headers["location"]
    assert push(client).json() == {"resultCode": 200, "resultMsg": "success"}
    assert push(client).json() == {"resultCode": 200, "resultMsg": "success"}
    assert push(client, role=True, version=101).status_code == 200
    assert login(client, ticket="ST-test-002").status_code == 303
    assert client.get("/api/v1/auth/session").json()["user"]["canAccessAdmin"] is True
    assert client.get("/api/v1/auth/session?scope=admin").status_code == 200
    with Session(engine) as db:
        user = db.scalar(select(User))
        assert user.iam_subject == "group-001" and user.role == "admin"
    logout_xml = (
        '<samlp:LogoutRequest xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol">'
        "<samlp:SessionIndex>ST-test-002</samlp:SessionIndex></samlp:LogoutRequest>"
    )
    assert (
        client.post("/api/v1/auth/single-logout", data={"logoutRequest": logout_xml}).status_code
        == 403
    )
    result = client.post(
        "/api/v1/auth/single-logout",
        data={"logoutRequest": logout_xml},
        headers={"X-Test-Proof": PROOF},
    )
    assert result.status_code == 200
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
    assert (
        client.post(USER_PATH, json=invalid_role, headers={"X-Test-Proof": PROOF}).status_code
        == 422
    )
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
    result = client.post(USER_PATH, json=payload, headers={"X-Test-Proof": PROOF})
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
