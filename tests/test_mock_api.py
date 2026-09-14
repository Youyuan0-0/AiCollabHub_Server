from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from minio import Minio
from redis import Redis
from sqlalchemy import Engine

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def clients(monkeypatch):
    # Mock API 即使配置了不可用的真实服务，也不允许读取或写入它们。
    connect = Mock(side_effect=AssertionError("Mock business routes must not access services"))
    monkeypatch.setattr(Engine, "connect", connect)
    monkeypatch.setattr(Redis, "execute_command", connect)
    monkeypatch.setattr(Minio, "_execute", connect)
    settings = Settings(
        database_url="postgresql+psycopg://unused@127.0.0.1/test",
        redis_url="redis://127.0.0.1:1/0",
        minio_endpoint="http://127.0.0.1:1",
        minio_access_key="unused",
        minio_secret_key="unused",
    )
    with (
        TestClient(create_app(settings.model_copy(update={"instance_id": "A"}))) as a,
        TestClient(create_app(settings.model_copy(update={"instance_id": "B"}))) as b,
    ):
        yield a, b
    connect.assert_not_called()


def test_all_pages_have_http_samples_without_authentication(clients):
    a, b = clients
    for path in (
        "/datasets",
        "/models",
        "/applications",
        "/tools",
        "/knowledge-bases",
        "/overview/home",
        "/overview/operations",
        "/conversations",
        "/feedback",
        "/admin/feedback",
        "/admin/overview",
    ):
        response = a.get("/api/v1" + path)
        assert response.status_code == 200, (path, response.text)
        assert response.headers["x-data-mode"] == "mock"
        assert response.json() == b.get("/api/v1" + path).json()
    assert a.get("/api/v1/framework").json()["instance"] == "A"
    assert b.get("/api/v1/framework").json()["instance"] == "B"
    assert a.get("/api/v1/auth/session").status_code == 404
    assert a.post("/api/v1/auth/login", json={}).status_code == 404
    assert a.post("/api/v1/files", content=b"test").status_code == 404


def test_catalog_combines_filters_and_paginates(clients):
    a, _ = clients
    first = a.get("/api/v1/datasets", params={"pageSize": 2}).json()
    second = a.get("/api/v1/datasets", params={"pageSize": 2, "page": 2}).json()
    assert len(first["items"]) == 2 and first["total"] > 2
    assert not {i["id"] for i in first["items"]}.intersection(i["id"] for i in second["items"])
    sample = first["items"][0]
    match = a.get("/api/v1/datasets", params={"q": sample["name"], "owner": sample["owner"]}).json()
    assert match["items"][0]["id"] == sample["id"]
    assert a.get("/api/v1/datasets?favoriteIds=[]").json()["total"] == 0
    assert a.get("/api/v1/datasets?facets=%5B1%5D").status_code == 422
    assert a.get("/api/v1/datasets?page=0").status_code == 422


def test_feedback_actions_echo_but_do_not_create_real_business_state(clients):
    a, b = clients
    original = a.get("/api/v1/feedback").json()
    response = a.post(
        "/api/v1/feedback",
        json={
            "type": "体验建议",
            "module": "数据集",
            "title": "接口示例",
            "description": "演示请求",
            "attachmentName": "demo.pdf",
        },
    )
    assert response.status_code == 201
    assert response.json()["title"] == "接口示例"
    assert b.get("/api/v1/feedback").json() == original
    changed = a.patch(
        "/api/v1/admin/feedback/" + original[0]["id"],
        json={"status": "已解决", "reply": "演示处理"},
    )
    assert changed.status_code == 200 and changed.json()["reply"] == "演示处理"
    assert a.get("/api/v1/feedback").json() == original


def test_chat_knowledge_and_tools_return_mock_contracts(clients):
    a, b = clients
    answer = a.post("/api/v1/conversations", json={"question": "测试", "model": "demo"})
    assert answer.status_code == 200 and "模拟回答" in answer.json()["text"]
    base = a.get("/api/v1/knowledge-bases").json()[0]
    assert b.get(f"/api/v1/knowledge-bases/{base['id']}/chunks").status_code == 200
    uploaded = a.post(
        f"/api/v1/knowledge-bases/{base['id']}/documents",
        json={"files": [{"name": "example.pdf", "size": 128}]},
    )
    assert uploaded.json()["documents"][0]["name"] == "example.pdf"
    assert b.get("/api/v1/knowledge-bases").json()[0] == base
    resource = a.get("/api/v1/tools").json()["items"][0]
    invocation = a.post(f"/api/v1/resources/{resource['id']}/invoke", json={"prompt": "测试"})
    assert invocation.status_code == 200 and "模拟响应" in invocation.json()["text"]


def test_production_does_not_mount_mock_routes(tmp_path):
    settings = Settings(app_env="production", database_url=f"sqlite:///{tmp_path / 'db'}")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/datasets").status_code == 404
        assert client.get("/api/v1/admin/feedback").status_code == 404
        assert client.get("/api/v1/health/live").status_code == 200
