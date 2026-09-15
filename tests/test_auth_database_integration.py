"""验证分支合并后，数据库业务与 Mock 业务使用相同的登录及 CSRF 边界。"""

import json

from test_auth import auth as auth
from test_auth import csrf, login


def test_database_routes_require_login_and_keep_health_public(auth):
    a, _, iam, _ = auth
    assert a.get("/api/v1/health/live").status_code == 200
    config = a.get("/api/v1/auth/config")
    assert config.status_code == 200
    assert config.headers["referrer-policy"] == "no-referrer"
    assert "x-data-mode" not in config.headers
    for path in ("overview/home", "overview/operations", "conversations"):
        response = a.get(f"/api/v1/{path}")
        assert response.status_code == 401
        assert response.json()["detail"] == "authentication_required"
    assert a.post("/api/v1/conversations/new", json={}).status_code == 401

    login(a, iam)
    for path in ("overview/home", "overview/operations", "conversations"):
        response = a.get(f"/api/v1/{path}")
        assert response.status_code == 200, response.text
        assert response.headers["x-data-mode"] == "development"
    assert a.get("/api/v1/applications").headers["x-data-mode"] == "mock"
    # 获得登录身份也不会开放尚未配置可信发布方的看板写入。
    assert a.put("/api/v1/overview/home", json={}).status_code == 403
    response = a.put("/api/v1/overview/home", json={}, headers=csrf(a))
    assert response.status_code == 503
    assert response.json()["detail"] == "overview_publication_not_configured"


def test_authenticated_conversations_validate_csrf_and_share_across_instances(auth):
    a, b, iam, _ = auth
    root = "/api/v1/conversations"
    login(a, iam)
    headers = csrf(a)
    b.cookies.update(a.cookies)
    payload = {"question": "合并后验证", "model": "演示模型", "knowledgeBase": ""}
    assert a.post(f"{root}/new", json={}).status_code == 403
    assert a.post(root, data={"input": json.dumps(payload)}).status_code == 403
    # multipart 旧入口仍可调用，新入口也必须使用同一套认证。
    legacy = a.post(root, data={"input": json.dumps(payload)}, headers=headers)
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["text"] == payload["question"]
    created = a.post(f"{root}/new", json={}, headers=headers)
    assert created.status_code == 201, created.text
    identifier = created.json()["id"]
    messages = f"{root}/{identifier}/messages"
    assert a.post(messages, json=payload).status_code == 403
    answered = a.post(messages, json=payload, headers=headers)
    assert answered.status_code == 200, answered.text
    assert answered.json()["text"] == payload["question"]
    assert len(b.get(messages).json()["messages"]) == 2
    assert b.delete(f"{root}/{identifier}").status_code == 403
    assert b.delete(f"{root}/{identifier}", headers=headers).status_code == 204
    assert a.get(messages).status_code == 404
    assert a.post("/api/v1/auth/logout?global_logout=false", headers=headers).status_code == 200
    assert b.get(root).status_code == 401
    assert b.get("/api/v1/overview/home").status_code == 401
