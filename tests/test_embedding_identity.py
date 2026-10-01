from test_auth import auth as auth
from test_auth import csrf, login


def test_portal_identity_header_does_not_authenticate_a_guest(auth):
    """门户传来的用户编号不能代替登录凭证。"""
    client, _, _, _ = auth
    assert (
        client.get("/api/v1/datasets", headers={"X-Expected-User": "mock-user"}).status_code == 401
    )


def test_portal_identity_must_match_authenticated_session(auth):
    """编号不一致阻止读取和写入，但不撤销合法会话。"""
    client, _, iam, _ = auth
    login(client, iam)
    assert (
        client.get("/api/v1/datasets", headers={"X-Expected-User": "mock-user"}).status_code == 200
    )
    wrong = {**csrf(client), "X-Expected-User": "mock-user-b"}
    response = client.get("/api/v1/datasets", headers=wrong)
    assert response.status_code == 409
    assert response.json()["detail"] == "identity_mismatch"
    assert (
        client.put(
            "/api/v1/models/example/favorite", headers=wrong, json={"favorite": True}
        ).status_code
        == 409
    )
    assert client.get("/api/v1/auth/session").json()["user"]["id"] == "mock-user"


def test_old_portal_identity_cannot_write_after_cookie_account_switch(auth):
    """会话换人后，即使 CSRF 已刷新，也不能按旧门户账号提交操作。"""
    client, _, iam, _ = auth
    login(client, iam)
    login(client, iam, subject="mock-user-b")
    headers = {**csrf(client), "X-Expected-User": "mock-user"}
    assert (
        client.put(
            "/api/v1/models/example/favorite", headers=headers, json={"favorite": True}
        ).status_code
        == 409
    )
    assert client.get("/api/v1/auth/session").json()["user"]["id"] == "mock-user-b"
