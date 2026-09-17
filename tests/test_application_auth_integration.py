"""登录与场景应用合并后，验证身份归属、CSRF 和管理端审核链路。"""

from test_auth import auth as auth
from test_auth import csrf, login


def test_publication_defaults_use_login_name_without_inventing_organization(request):
    client, _, iam, _ = request.getfixturevalue("auth")
    path = "/api/v1/application-publication-defaults"
    assert client.get(path).status_code == 401
    login(client, iam, subject="mock-user")
    defaults = client.get(path).json()
    assert defaults == {
        "contact": "模拟用户A",
        "owner": "",
        "organizationId": "",
        "ownerAvailable": False,
    }
    draft = client.post(
        "/api/v1/application-publications", json={"data": {"name": "新建"}}, headers=csrf(client)
    ).json()
    assert draft["data"]["contact"] == defaults["contact"]
    assert draft["data"]["owner"] == ""


def test_publications_use_login_subject_and_reject_demo_identity(request):
    client, _, iam, _ = request.getfixturevalue("auth")
    path = "/api/v1/application-publications"
    assert client.get(path).status_code == 401
    login(client, iam, subject="mock-user")
    client.cookies.set("application_user", "forged-demo-cookie")
    data = {"name": "登录联动应用", "version": "1.0", "category": "智能问答"}
    assert client.post(path, json={"data": data}).status_code == 403
    saved = client.post(path, json={"data": data}, headers=csrf(client))
    assert saved.status_code == 201, saved.text
    record = saved.json()
    assert record["createdBy"] == "mock-user"
    assert record["createdByName"] == "模拟用户A"
    session = client.get("/api/v1/application-session").json()
    assert session["current"]["id"] == "mock-user"
    assert session["actors"] == []
    assert session["current"]["projectIds"] == []
    assert (
        client.post(
            "/api/v1/application-demo-session",
            json={"actorId": "demo-admin", "audience": "user"},
            headers=csrf(client),
        ).status_code
        == 404
    )
    login(client, iam, subject="mock-admin", scope="user")
    assert client.get(path).json() == []
    assert client.get(f"{path}/{record['id']}").status_code == 404
    assert (
        client.put(
            f"{path}/{record['id']}",
            json={"data": data, "revision": record["revision"]},
            headers=csrf(client),
        ).status_code
        == 403
    )


def test_login_publication_can_be_reviewed_with_admin_session(request):
    client, _, iam, _ = request.getfixturevalue("auth")
    login(client, iam)
    saved = client.post(
        "/api/v1/application-publications",
        json={
            "submit": True,
            "data": {
                "name": "统一登录审核应用",
                "version": "1.0",
                "category": "文档创作",
                "owner": "研发单位",
                "summary": "验证统一登录后的发布和审核流程",
                "triggerType": "手动触发",
                "communicationProtocol": "HTTP",
                "deployment": "工具链托管",
            },
        },
        headers=csrf(client),
    )
    assert saved.status_code == 201, saved.text
    record = saved.json()
    assert client.get("/api/v1/admin/application-session").status_code == 403
    login(client, iam, subject="mock-admin", scope="admin")
    session = client.get("/api/v1/admin/application-session")
    assert session.status_code == 200, session.text
    assert session.json()["current"]["id"] == "mock-admin"
    assert session.json()["current"]["role"] == "admin"
    # 从管理端登录后，用户端沿用同一管理员身份。
    assert client.get("/api/v1/application-session").json()["current"]["id"] == "mock-admin"
    assert client.get("/api/v1/admin/application-dependencies").status_code == 200
    reviewed = client.post(
        f"/api/v1/admin/application-publications/{record['id']}/review",
        json={
            "decision": "approved",
            "revision": record["revision"],
            "scores": {"completeness": 4, "usability": 4, "security": 4},
        },
        headers=csrf(client, "admin"),
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["reviews"][-1]["reviewerId"] == "mock-admin"
    login(client, iam, subject="mock-user")
    assert client.get(f"/api/v1/applications/{record['applicationId']}").status_code == 200
    assert (
        client.get(f"/api/v1/application-publications/{record['id']}").json()["status"]
        == "approved"
    )
