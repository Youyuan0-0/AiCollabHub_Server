"""仅单点登录模式：首次建号、本地运维授权与共享会话的完整回归。"""

import sys
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session
from test_iam_preparation import USER_PATH, login
from test_iam_preparation import iam_app as iam_app

from app.modules.common.auth.local_roles import set_local_role
from app.modules.common.auth.models import AuthSession, User, UserRole, UserStatus
from app.modules.common.system_logs import SystemLog
from scripts import set_user_role

pytestmark = pytest.mark.parametrize("iam_app", [False], indirect=True)


def assign_role(client, engine, role, *, subject="group-001"):
    with Session(engine) as db:
        return set_local_role(
            db,
            client.app.state.settings,
            work_no=subject,
            role=UserRole(role),
            operator="test-operator",
            host="test-host",
        )


def test_first_login_creates_local_user_and_no_sync_routes(iam_app):
    client, engine = iam_app
    assert client.post(USER_PATH, json={}).status_code == 404
    assert client.post("/api/v1/auth/mock/identity-sync", json={}).status_code == 404
    assert login(client).headers["location"] == "http://testserver/"
    with Session(engine) as db:
        user = db.scalar(select(User))
        user_id = user.id
        assert user.iam_subject == "group-001"
        assert user.display_name == "集团用户"
        assert user.status == UserStatus.ACTIVE
        assert user.role == UserRole.USER
        assert user.user_sync_time is None and user.role_sync_time is None
    client.cookies.clear()
    login(client, ticket="ST-next-login")
    with Session(engine) as db:
        assert [user.id for user in db.scalars(select(User))] == [user_id]


def test_first_admin_entry_persists_ordinary_identity_without_admin_access(iam_app):
    client, engine = iam_app
    result = login(client, scope="admin")
    assert "admin_not_allowed" in result.headers["location"]
    assert client.get("/api/v1/auth/session").status_code == 200
    assert client.get("/api/v1/auth/session?scope=admin").status_code == 403
    assert assign_role(client, engine, "admin") is True
    assert client.get("/api/v1/auth/session?scope=admin").status_code == 200


def test_local_grant_revoke_and_relogin_preserve_roles_and_audit(iam_app):
    client, engine = iam_app
    login(client)
    assert assign_role(client, engine, "admin") is True
    assert assign_role(client, engine, "admin") is False
    assert client.get("/api/v1/auth/session?scope=admin").status_code == 200
    client.cookies.clear()
    login(client, scope="admin", ticket="ST-admin-login")
    assert client.get("/api/v1/auth/session?scope=admin").status_code == 200
    assert assign_role(client, engine, "user") is True
    assert client.get("/api/v1/auth/session?scope=admin").status_code == 403
    assert client.get("/api/v1/auth/session").status_code == 200
    client.cookies.clear()
    login(client, ticket="ST-after-revoke")
    assert client.get("/api/v1/auth/session").json()["user"]["canAccessAdmin"] is False
    with Session(engine) as db:
        logs = db.scalars(
            select(SystemLog).where(SystemLog.event == "auth_local_role_changed")
        ).all()
        assert len(logs) == 2
        assert {entry.details["role"] for entry in logs} == {"user", "admin"}
        assert all(entry.details["operator"] == "test-operator" for entry in logs)
        assert all(entry.details["workNo"] == "group-001" for entry in logs)


@pytest.mark.parametrize("status", ["disabled", "deleted", "pending"])
def test_login_does_not_reactivate_existing_blocked_identity(iam_app, status):
    client, engine = iam_app
    with Session(engine) as db:
        db.add(User(iam_subject="group-001", status=UserStatus(status), role=UserRole.USER))
        db.commit()
    assert "account_unavailable" in login(client).headers["location"]
    with Session(engine) as db:
        assert db.scalar(select(User)).status == status
        assert db.scalar(select(AuthSession)) is None
    with pytest.raises(ValueError, match="正常启用"):
        assign_role(client, engine, "admin")


def test_invalid_ticket_cannot_create_user(iam_app):
    client, engine = iam_app
    assert "invalid_ticket" in login(client, ticket="ST-invalid").headers["location"]
    with Session(engine) as db:
        assert db.scalar(select(User)) is None


def test_default_logout_revokes_shared_session_and_exits_iam(iam_app):
    client, _ = iam_app
    login(client)
    session = client.get("/api/v1/auth/session").json()
    result = client.post(
        "/api/v1/auth/logout",
        headers={"Origin": "http://testserver", "X-CSRF-Token": session["csrfToken"]},
    )
    target = urlsplit(result.json()["redirectUrl"])
    assert target.netloc == "iam.test"
    assert target.path == "/authn-api/v5/cas/assigned/logout"
    assert parse_qs(target.query)["service"] == ["http://testserver/login?logged_out=1"]
    assert client.get("/api/v1/auth/session").status_code == 401
    assert client.get("/api/v1/auth/session?scope=admin").status_code == 401


def test_cli_refuses_unknown_users_and_sync_authority(iam_app):
    client, engine = iam_app
    with pytest.raises(ValueError, match="用户不存在"):
        assign_role(client, engine, "admin")
    with Session(engine) as db:
        assert db.scalar(select(User)) is None
    login(client)
    client.app.state.settings.auth_identity_sync_enabled = True
    with pytest.raises(ValueError, match="身份同步已启用"):
        assign_role(client, engine, "admin")


def test_audit_failure_rolls_back_role_change(iam_app):
    client, engine = iam_app
    login(client)

    def reject_audit(mapper, connection, target):
        raise RuntimeError("audit unavailable")

    event.listen(SystemLog, "before_insert", reject_audit)
    try:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            assign_role(client, engine, "admin")
    finally:
        event.remove(SystemLog, "before_insert", reject_audit)
    with Session(engine) as db:
        assert db.scalar(select(User)).role == UserRole.USER
        assert (
            db.scalar(select(SystemLog).where(SystemLog.event == "auth_local_role_changed")) is None
        )


def test_role_command_grant_noop_revoke_and_unknown_user(iam_app, monkeypatch, capsys):
    client, _ = iam_app
    login(client)
    monkeypatch.setattr(set_user_role, "Settings", lambda: client.app.state.settings)
    for role, expected in [("admin", "已更新"), ("admin", "无需修改"), ("user", "已更新")]:
        monkeypatch.setattr(
            sys, "argv", ["set_user_role", "--work-no", "group-001", "--role", role]
        )
        set_user_role.main()
        assert expected in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["set_user_role", "--work-no", "missing", "--role", "admin"])
    with pytest.raises(SystemExit) as error:
        set_user_role.main()
    assert error.value.code == 2
    assert "用户不存在" in capsys.readouterr().err
