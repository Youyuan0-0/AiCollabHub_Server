"""首页合并后仍须通过登录及 CSRF 检查，历史和看板继续使用原接口。"""

import pytest
from test_auth import auth as auth
from test_auth import csrf, login


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/overview/home"),
        ("GET", "/overview/operations"),
        ("GET", "/conversations"),
        ("GET", "/conversations/example/messages"),
        ("POST", "/conversations/new"),
        ("POST", "/conversations/example/messages"),
        ("DELETE", "/conversations/example"),
        # 新附件端点必须与已有首页业务共用登录依赖。
        ("POST", "/conversations/example/attachments"),
        ("GET", "/conversations/example/attachments/attachment-example/content"),
    ],
)
def test_home_endpoints_require_session(request, method, path):
    client, _, _, _ = request.getfixturevalue("auth")
    assert client.request(method, "/api/v1" + path).status_code == 401


def test_logged_in_home_conversation_and_csrf(request):
    client, _, iam, _ = request.getfixturevalue("auth")
    login(client, iam)
    # SQLite 仅验证会话边界，首页新基线在 PostgreSQL 业务测试中验证。
    assert client.get("/api/v1/overview/home").status_code == 503
    assert client.get("/api/v1/overview/operations").status_code == 200
    root = "/api/v1/conversations"
    assert client.post(root + "/new", json={}).status_code == 403
    created = client.post(root + "/new", json={}, headers=csrf(client))
    assert created.status_code == 201, created.text
    path = f"{root}/{created.json()['id']}"
    # 上传即使携带有效登录 Cookie，也不能跳过写请求的 CSRF 验证。
    assert client.post(path + "/attachments", files={"file": ("a.txt", b"a")}).status_code == 403
    data = {"question": "合并后保留多轮问答", "model": "测试模型", "knowledgeBase": ""}
    assert client.post(path + "/messages", json=data).status_code == 403
    sent = client.post(path + "/messages", json=data, headers=csrf(client))
    assert sent.status_code == 200, sent.text
    history = client.get(path + "/messages")
    assert history.status_code == 200
    assert len(history.json()["messages"]) == 2
    assert client.delete(path).status_code == 403
    assert client.delete(path, headers=csrf(client)).status_code == 204
    assert client.get(path + "/messages").status_code == 404
