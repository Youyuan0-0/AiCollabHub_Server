from copy import deepcopy

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.modules.common.identity import ACTORS
from app.modules.datasets.repository import DatasetRepository
from app.modules.datasets.schemas import DatasetInput, SaveDataset
from app.modules.datasets.workspace import DatasetWorkspace


@pytest.fixture
def client():
    """使用独立应用实例验证用户端 HTTP 契约，不连接外部服务。"""
    with TestClient(create_app(Settings())) as client:
        yield client


@pytest.fixture
def data():
    """构造完整发布资料，同时覆盖任务表要求的多选分类及单选用途。"""
    return DatasetInput(
        name="试验数据集",
        englishName="Test Dataset",
        summary="试验脱敏数据",
        owner="试验单位",
        contact="测试联系人",
        dataApplication=["智能试验", "新增类型"],
        dataShape=["结构化数据", "时序"],
        aiPurpose="评测数据集",
        fieldSchema='{"type":"object","properties":{"speed":{"type":"number"}}}',
        language=["中文"],
        dataFormat=["CSV"],
        dataLevel="一般数据",
        descriptionMarkdown="## 来源\n脱敏试验样例",
        sampleData="speed\n100",
        recordCount=1,
    ).model_dump(by_alias=True)


def test_publication_roundtrip_validation_and_pending_readonly(client, data):
    """草稿可缺项，提交必须完整；系统编号稳定，待审记录不进入目录。"""
    draft = client.post("/api/v1/dataset-publications", json={"data": {}})
    assert draft.status_code == 201
    record = draft.json()
    path = f"/api/v1/dataset-publications/{record['id']}"
    assert client.get(path).json() == record
    assert client.put(path, json={"data": {}, "submit": True, "revision": 1}).status_code == 422
    assert client.get(path).json()["revision"] == 1
    updated = client.put(path, json={"data": data, "revision": 1}).json()
    assert updated["data"] == data
    assert updated["datasetId"] == record["datasetId"]
    assert updated["createdAt"] == record["createdAt"]
    assert updated["revision"] == 2
    assert client.put(path, json={"data": data, "revision": 1}).status_code == 409
    pending = client.put(path, json={"data": data, "submit": True, "revision": 2}).json()
    assert pending["status"] == "pending" and pending["submittedAt"]
    assert client.put(path, json={"data": data, "revision": 3}).status_code == 409
    assert client.get("/api/v1/datasets", params={"q": data["name"]}).json()["total"] == 0
    assert client.get(f"/api/v1/datasets/{record['datasetId']}").status_code == 404


@pytest.mark.parametrize("schema", ["[]", '{"type":"object"}', '{"type":"nonsense"}', "broken"])
def test_submission_rejects_invalid_field_schema(client, data, schema):
    """字段清单必须是包含字段的合法对象 Schema，不能只检查 JSON 能否解析。"""
    data["fieldSchema"] = schema
    assert (
        client.post("/api/v1/dataset-publications", json={"data": data, "submit": True}).status_code
        == 422
    )


def test_identity_and_statistics_are_server_controlled(client, data):
    """客户端不能伪造状态或归属身份，收藏幂等，样例下载才增加下载量。"""
    assert (
        client.post(
            "/api/v1/dataset-publications", json={"data": data, "status": "approved"}
        ).status_code
        == 422
    )
    resource = client.get("/api/v1/datasets").json()["items"][0]
    path = f"/api/v1/datasets/{resource['id']}"
    for _ in range(2):
        assert client.put(path + "/favorite", json={"favorite": True}).json()["favoriteCount"] == 1
    assert client.get("/api/v1/dataset-activity").json()["favoriteIds"] == [resource["id"]]
    result = client.post(path + "/sample-download")
    assert result.status_code == 200 and result.json()["sample"] == resource["preview"]
    assert client.get(path).json()["downloadCount"] == 1
    sorted_page = client.get("/api/v1/datasets", params={"sort": "recent_download"}).json()
    assert sorted_page["items"][0]["id"] == resource["id"]
    assert client.put(path + "/favorite", json={"favorite": False}).json()["favoriteCount"] == 0
    assert client.get("/api/v1/dataset-activity").json()["favoriteIds"] == []


def test_repository_isolation_conflicts_and_reopen(data):
    """内存测试替身隔离账号并拒绝过期更新；持久化由新基线集成测试覆盖。"""
    repository = DatasetRepository()
    workspace = DatasetWorkspace(repository)
    owner, reader = ACTORS[0], ACTORS[2]
    created = workspace.save(SaveDataset(data=DatasetInput(**data)), owner)
    assert workspace.publications(reader) == []
    with pytest.raises(HTTPException) as error:
        workspace.publication(created["id"], reader)
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        workspace.save(SaveDataset(data=DatasetInput(**data)), reader)
    assert error.value.status_code == 403
    workspace.interact("ds-fault-v3", owner, favorite=True)
    assert workspace.activity(reader)["favoriteIds"] == []
    snapshot = deepcopy(created)
    snapshot["data"]["name"] = "不会直接修改存储"
    assert workspace.publication(created["id"], owner)["data"]["name"] == data["name"]
    updated = repository.save("publication:" + created["id"], created, 1)
    assert updated["revision"] == 2
    with pytest.raises(HTTPException) as error:
        repository.save("publication:" + created["id"], created, 1)
    assert error.value.status_code == 409
