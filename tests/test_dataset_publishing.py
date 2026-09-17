"""本地审核上架、历史补发及第三方通道边界回归。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from test_auth import auth as auth
from test_auth import csrf, login
from test_dataset_admin import SCORES
from test_dataset_workspace import client as client
from test_dataset_workspace import data as data

from app.core.config import Settings
from app.main import create_app
from app.modules.datasets.publishing import (
    ExternalDatasetPublisher,
    LocalDatasetPublisher,
    sample_preview,
)
from app.modules.datasets.repository import DatasetRecord


def approve(client, record):
    """通过真实审核接口提交七维评分，不直接篡改业务状态。"""
    return client.post(
        "/api/v1/admin/dataset-publications/" + record["id"] + "/review",
        json={"decision": "approved", "revision": record["revision"], "scores": SCORES},
    )


def test_approval_publishes_catalog_preview_and_engagement(client, data):
    """通过后即可检索详情、下载公开样例和评分，内部管理字段不得泄露。"""
    record = client.post("/api/v1/dataset-publications", json={"data": data, "submit": True}).json()
    path = "/api/v1/datasets/" + record["datasetId"]
    assert client.get(path).status_code == 404
    response = approve(client, record)
    assert response.status_code == 200
    assert response.json()["syncStatus"] == "local_published"
    detail = client.get(path).json()
    assert detail["name"] == data["name"] and detail["publicationSource"] == "local"
    assert detail["preview"] == {"columns": ["speed"], "rows": [["100"]]}
    assert detail["versions"][0]["version"] == data["version"]
    assert not {"contact", "dataLevel", "createdBy", "organizationId", "sampleData"} & detail.keys()
    assert client.get("/api/v1/datasets", params={"q": data["name"]}).json()["total"] == 1
    assert client.post(path + "/sample-download").status_code == 200
    assert client.put(path + "/favorite", json={"favorite": True}).status_code == 200
    assert (
        client.put(path + "/rating", json={"rating": 5, "comment": "已发布数据集评价"}).status_code
        == 200
    )
    updated = client.get(path).json()
    assert (updated["downloadCount"], updated["favoriteCount"], updated["rating"]) == (1, 1, 5)
    # 新上架记录继承可信组织，单位管理员可以管理本单位范围；范围撤销立即作用于目录。
    response = client.put(
        "/api/v1/admin/datasets/" + record["datasetId"] + "/visibility",
        json={"scope": "organizations", "targets": ["org-production"], "revision": 0},
    )
    assert response.status_code == 200
    assert client.get(path).status_code == 404


def test_old_approved_record_can_be_published_once_without_new_review(client, data):
    """旧待同步申请显式补发，重复提交幂等且不会增加审核历史或创建重复资源。"""
    workspace = client.app.state.dataset_workspace
    workspace.publisher = ExternalDatasetPublisher()
    record = client.post("/api/v1/dataset-publications", json={"data": data, "submit": True}).json()
    approved = approve(client, record).json()
    path = "/api/v1/admin/dataset-publications/" + record["id"] + "/publish"
    assert approved["syncStatus"] == "pending"
    assert client.post(path, json={"revision": 2}).status_code == 503
    workspace.publisher = LocalDatasetPublisher()
    assert client.post(path, json={"revision": 1}).status_code == 409
    published = client.post(path, json={"revision": 2}).json()
    assert published["syncStatus"] == "local_published"
    assert published["reviews"] == approved["reviews"]
    assert client.post(path, json={"revision": 3}).json() == published
    assert client.get("/api/v1/datasets", params={"q": data["name"]}).json()["total"] == 1
    workspace.publisher = ExternalDatasetPublisher()
    assert client.get("/api/v1/datasets/" + record["datasetId"]).status_code == 200


def test_pending_cannot_bypass_review_and_failed_save_does_not_publish(client, data, monkeypatch):
    """不允许跳过审核补发，事务提交失败时目录和审核记录均保持原状。"""
    record = client.post("/api/v1/dataset-publications", json={"data": data, "submit": True}).json()
    path = "/api/v1/admin/dataset-publications/" + record["id"]
    assert client.post(path + "/publish", json={"revision": 1}).status_code == 409

    def failed_save(*args, **kwargs):
        raise RuntimeError("simulated transaction failure")

    monkeypatch.setattr(client.app.state.dataset_workspace.repository, "save", failed_save)
    with pytest.raises(RuntimeError, match="transaction failure"):
        approve(client, record)
    assert client.get(path).json() == record
    assert client.get("/api/v1/datasets/" + record["datasetId"]).status_code == 404


def test_publication_and_ownership_survive_database_app_restart(tmp_path, data):
    """实例重建后上架资料仍来自数据库快照，已发布时间和版本不会重新生成。"""
    url = f"sqlite:///{tmp_path / 'publish.db'}"
    engine = create_engine(url)
    DatasetRecord.__table__.create(engine)
    settings = Settings(database_url=url, datasets_storage="database")
    with TestClient(create_app(settings)) as first:
        record = first.post(
            "/api/v1/dataset-publications", json={"data": data, "submit": True}
        ).json()
        assert approve(first, record).status_code == 200
        resource = first.get("/api/v1/datasets/" + record["datasetId"]).json()
    with TestClient(create_app(settings)) as second:
        assert second.get("/api/v1/datasets/" + record["datasetId"]).json() == resource
        second.post(
            "/api/v1/application-demo-session",
            json={"actorId": "demo-org-admin", "audience": "admin"},
        )
        detail = second.get("/api/v1/admin/datasets/" + record["datasetId"]).json()
        assert detail["canManageVisibility"] is True
    engine.dispose()


def test_publish_requires_admin_scope_and_csrf(auth, data):
    """历史补发复用认证权限，普通用户与缺少 CSRF 的管理请求都不能修改状态。"""
    client, _, iam, _ = auth
    login(client, iam)
    record = client.post(
        "/api/v1/dataset-publications", json={"data": data, "submit": True}, headers=csrf(client)
    ).json()
    path = "/api/v1/admin/dataset-publications/" + record["id"] + "/publish"
    assert client.post(path, json={"revision": 1}, headers=csrf(client)).status_code == 403
    login(client, iam, scope="admin", subject="mock-admin")
    assert client.post(path, json={"revision": 1}).status_code == 403


@pytest.mark.parametrize(
    "sample,columns,rows",
    [
        ("", [], []),
        ("单行文字", ["公开样例"], [["单行文字"]]),
        ("a,b\n1,2", ["a", "b"], [["1", "2"]]),
        ('[{"a":1},{"a":2}]', ["a"], [["1"], ["2"]]),
    ],
)
def test_sample_preview_preserves_reviewed_content(sample, columns, rows):
    """支持表格、JSON 和自由文本样例，空样例不伪造数据。"""
    assert sample_preview(sample) == {"columns": columns, "rows": rows}
