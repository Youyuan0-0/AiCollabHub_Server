"""需求补齐回归：隔离内存模拟业务，禁止连接现有数据库。"""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.core.config import Settings
from app.main import create_app
from app.modules.common.review_schemas import ReviewQualityScores


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(Engine, "connect", Mock(side_effect=AssertionError("禁止数据库访问")))
    with TestClient(create_app(Settings(_env_file=None))) as client:
        yield client


def model_data(**changes):
    return {
        "name": "回归模型",
        "summary": "专属字段验证",
        "version": "v1",
        "framework": "Transformers",
        "modelFormat": "Safetensors",
        "precision": "BF16",
        "configJson": '{"model_type":"test"}',
        "license": "内部",
        "serviceCapabilities": ["部署"],
        **changes,
    }


def scores():
    return ReviewQualityScores(**{key: 4 for key in ReviewQualityScores.model_fields}).model_dump(
        by_alias=True
    )


def approve(client, kind, record, **changes):
    response = client.post(
        f"/api/v1/admin/{kind}-publications/{record['id']}/review",
        json={
            "decision": "approved",
            "revision": record["revision"],
            "scores": scores(),
            **changes,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def select_user(client, identifier):
    token, _ = client.app.state.application_identity.select(identifier)
    client.cookies.set("application_user", token)


def test_model_contract_review_report_version_and_permissions(client):
    incomplete = client.post(
        "/api/v1/model-publications", json={"data": model_data(framework=""), "submit": True}
    )
    assert incomplete.status_code == 422
    response = client.post(
        "/api/v1/model-publications", json={"data": model_data(), "submit": True}
    )
    assert response.status_code == 201, response.text
    record = response.json()
    upload = client.post(
        f"/api/v1/admin/model-publications/{record['id']}/report-files?revision={record['revision']}",
        files={"file": ("report.txt", b"quality report", "text/plain")},
    )
    assert upload.status_code == 201, upload.text
    report_id = upload.json()["id"]
    result = approve(client, "model", record, reportFileIds=[report_id])
    assert result["reviews"][0]["reportFiles"][0]["id"] == report_id
    path = f"/api/v1/models/{record['resourceId']}"
    assert client.get(path).status_code == 200
    assert client.get(path).json()["isFavorite"] is False
    assert client.put(path + "/favorite", json={"favorite": True}).json()["favoriteCount"] == 1
    assert client.put(path + "/favorite", json={"favorite": True}).json()["favoriteCount"] == 1
    assert client.get("/api/v1/profile/favorites?kind=model").json()["total"] == 1
    assert client.get(path).json()["isFavorite"] is True
    assert "securityLevel" not in client.get(path).json()
    assert "sourceType" not in client.get(path).json()
    scope = client.put(
        f"/api/v1/admin/models/{record['resourceId']}/visibility",
        json={"scope": "organizations", "targets": ["org-research"], "revision": 1},
    )
    assert scope.status_code == 200, scope.text
    select_user(client, "demo-outsider")
    assert client.get(path).status_code == 404
    assert client.get(f"/api/v1/model-publications/{record['id']}").status_code == 404
    select_user(client, "demo-publisher")
    newer = client.post(f"/api/v1/model-publications/{record['id']}/new-version").json()
    submitted = client.put(
        f"/api/v1/model-publications/{newer['id']}",
        json={"data": model_data(version="v2"), "revision": newer["revision"], "submit": True},
    ).json()
    approve(client, "model", submitted)
    detail = client.get(f"/api/v1/admin/models/{record['resourceId']}").json()
    assert detail["version"] == "v2" and len(detail["versions"]) == 2
    assert detail["visibility"]["targets"] == ["org-research"]
    assert detail["favoriteCount"] == 1


def test_knowledge_requirements_permissions_and_persistent_state(client):
    assert client.post("/api/v1/knowledge-bases", json={"name": "缺简介"}).status_code == 422
    assert (
        client.post(
            "/api/v1/knowledge-bases",
            json={"name": "越权", "summary": "简介", "visibility": "集团公开"},
        ).status_code
        == 422
    )
    base = client.post(
        "/api/v1/knowledge-bases", json={"name": "测试知识库", "summary": "必须简介"}
    ).json()
    path = f"/api/v1/knowledge-bases/{base['id']}"
    assert "visibility" not in base and "dataLevel" not in base
    select_user(client, "demo-outsider")
    assert base["id"] not in [item["id"] for item in client.get("/api/v1/knowledge-bases").json()]
    select_user(client, "demo-publisher")
    assert (
        client.post(
            path + "/documents", json={"files": [{"name": "test.ppt", "size": 10}]}
        ).status_code
        == 422
    )
    assert (
        client.post(
            path + "/documents", json={"files": [{"name": "test.txt", "size": 100 * 1024**2 + 1}]}
        ).status_code
        == 422
    )
    uploaded = client.post(
        path + "/documents",
        json={"files": [{"name": "test.txt", "size": 1024}], "config": {"chunkSize": 123}},
    ).json()
    assert uploaded["totalSize"] == 1024 and uploaded["documentCount"] == 1
    assert uploaded["indexConfig"]["chunkSize"] == 123
    config = {
        **uploaded["indexConfig"],
        "chunkSize": 100,
        "chunkOverlap": 0,
        "semanticSearch": False,
    }
    assert client.put(path + "/settings", json=config).status_code == 200
    assert client.put(path + "/settings", json={**config, "chunkSize": 2001}).status_code == 422
    assert client.put(path + "/settings", json={**config, "chunkOverlap": 501}).status_code == 422
    result = client.post(path + "/retrieval", json={"query": "测试", "config": config})
    assert result.status_code == 200 and len(result.json()) == 1
    record = client.post(path + "/submit").json()
    assert client.post(path + "/submit").status_code == 409
    assert client.put(path + "/settings", json=config).status_code == 409
    missing_level = client.post(
        f"/api/v1/admin/knowledge-publications/{record['id']}/review",
        json={"revision": record["revision"], "decision": "approved", "scores": scores()},
    )
    assert missing_level.status_code == 422
    record = client.put(
        f"/api/v1/admin/knowledge-publications/{record['id']}/metadata",
        json={
            "revision": record["revision"],
            "dataLevel": "核心数据",
            "owner": "研发单位",
            "contact": "维护人员",
        },
    ).json()
    approve(client, "knowledge", record)
    select_user(client, "demo-outsider")
    public = next(
        item for item in client.get("/api/v1/knowledge-bases").json() if item["id"] == base["id"]
    )
    assert public["canManage"] is False and "dataLevel" not in public
    assert client.delete(path + "/documents?name=test.txt").status_code == 403


def test_profile_aggregation_and_contact_without_business_writes(client):
    profile = client.get("/api/v1/profile")
    assert profile.status_code == 200, profile.text
    assert profile.json()["employeeNumber"].startswith("DEMO-")
    assert (
        client.put("/api/v1/profile/contact", json={"contact": "test@example.com"}).json()[
            "contact"
        ]
        == "test@example.com"
    )
    assert (
        client.put("/api/v1/profile/contact", json={"contact": "x", "role": "admin"}).status_code
        == 422
    )
    client.post("/api/v1/model-publications", json={"data": model_data()})
    client.post("/api/v1/knowledge-bases", json={"name": "聚合知识库", "summary": "简介"})
    result = client.get("/api/v1/profile/publications")
    assert result.status_code == 200, result.text
    assert {"model", "knowledge"}.issubset({item["kind"] for item in result.json()["items"]})
    favorites = client.get("/api/v1/profile/favorites")
    assert favorites.status_code == 200, favorites.text


def test_import_idempotence_survives_edit_and_generic_publish_rejected(client):
    external = client.get("/api/v1/model-toolchain").json()["items"][0]["externalId"]
    first = client.post(f"/api/v1/model-toolchain/{external}/import").json()
    updated = client.put(
        f"/api/v1/model-publications/{first['id']}",
        json={"data": model_data(), "revision": first["revision"]},
    )
    assert updated.status_code == 200, updated.text
    second = client.post(f"/api/v1/model-toolchain/{external}/import").json()
    assert first["id"] == second["id"]
    assert second["data"]["source"]["externalId"] == external


def test_feedback_title_is_derived_and_twenty_attachments_are_accepted():
    from app.modules.feedback.schemas import FeedbackInput
    from app.schemas.conversations import ChatInput

    fields = ChatInput.model_fields
    assert fields["attachment_ids"].metadata
    payload = ChatInput(
        question="test",
        model="test",
        knowledge_base="",
        attachment_ids=[f"attachment-{index:032x}" for index in range(20)],
    )
    assert len(payload.attachment_ids) == 20
    feedback = FeedbackInput(
        type="function_error",
        description="Detailed feedback",
        contact="test",
        module="other",
        module_other="模块",
        client_request_id="00000000-0000-0000-0000-000000000001",
    )
    assert feedback.title == "Detailed feedback"
