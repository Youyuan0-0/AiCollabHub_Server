from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.core.config import Settings
from app.main import create_app
from app.modules.common.fixtures import fixture
from app.modules.datasets.repository import DatasetRecord


@pytest.fixture
def client():
    """以独立演示实例验证数据集排行，不调用第三方或连接真实数据库。"""
    with TestClient(create_app(Settings())) as client:
        yield client


def test_rankings_share_catalog_statistics_and_isolate_users(client):
    """下载榜、收藏榜和卡片保持同一口径，收藏按可信用户去重并支持取消。"""
    dataset_id = "ds-fault-v3"
    path = f"/api/v1/datasets/{dataset_id}"
    for _ in range(2):
        assert client.put(path + "/favorite", json={"favorite": True}).status_code == 200
        assert client.post(path + "/sample-download").status_code == 200
    token, _ = client.app.state.application_identity.select("demo-reader")
    client.cookies.set("application_user", token)
    assert client.get("/api/v1/dataset-activity").json()["favoriteIds"] == []
    assert client.put(path + "/favorite", json={"favorite": True}).json()["favoriteCount"] == 2
    for metric in ("downloads", "favorites"):
        response = client.get("/api/v1/datasets/rankings", params={"metric": metric, "limit": 1})
        assert response.status_code == 200
        ranking = response.json()
        assert ranking["metric"] == metric and ranking["downloadScope"] == "sample"
        item = ranking["items"][0]
        detail = client.get(path).json()
        assert item == {
            "rank": 1,
            "id": dataset_id,
            "name": detail["name"],
            "owner": detail["owner"],
            "downloadCount": 2,
            "favoriteCount": 2,
        }
        catalog = client.get("/api/v1/datasets", params={"sort": metric, "pageSize": 1}).json()
        assert catalog["items"][0]["id"] == dataset_id
    for _ in range(2):
        assert client.put(path + "/favorite", json={"favorite": False}).json()["favoriteCount"] == 1
    assert (
        client.get("/api/v1/datasets/rankings?metric=favorites&limit=1").json()["items"][0][
            "favoriteCount"
        ]
        == 1
    )


@pytest.mark.parametrize(
    "params", [{"metric": "usage"}, {"limit": 0}, {"limit": 101}, {"limit": 1.5}]
)
def test_ranking_rejects_invalid_parameters(client, params):
    """只接受已维护的指标和有界整数条数，防止无意读取整个目录。"""
    assert client.get("/api/v1/datasets/rankings", params=params).status_code == 422


def test_zero_counts_stable_order_and_missing_samples(client, monkeypatch):
    """初始统计不使用固定 usage，未提供样例的失败下载不计数，空目录返回空榜。"""
    resources = fixture("datasets", "resources")
    ranking = client.get("/api/v1/datasets/rankings?limit=100").json()["items"]
    assert [item["id"] for item in ranking] == sorted(item["id"] for item in resources)
    assert all(item["downloadCount"] == item["favoriteCount"] == 0 for item in ranking)
    for item in resources:
        item.pop("preview", None)
    monkeypatch.setattr("app.modules.datasets.workspace.fixture", lambda *_: deepcopy(resources))
    assert client.post(f"/api/v1/datasets/{resources[0]['id']}/sample-download").status_code == 404
    assert (
        client.put("/api/v1/datasets/missing/favorite", json={"favorite": True}).status_code == 404
    )
    assert client.get("/api/v1/datasets/rankings?limit=100").json()["items"] == ranking
    resources.clear()
    assert client.get("/api/v1/datasets/rankings").json()["items"] == []


def test_ranking_statistics_survive_database_app_restart(tmp_path):
    """临时数据库跨应用保留统计，复用数据集表，不创建模型表或正式资源资料。"""
    url = f"sqlite:///{tmp_path / 'datasets.db'}"
    engine = create_engine(url)
    DatasetRecord.__table__.create(engine)
    engine.dispose()
    settings = Settings(datasets_storage="database", database_url=url)
    with TestClient(create_app(settings)) as first:
        assert (
            first.put("/api/v1/datasets/ds-fault-v3/favorite", json={"favorite": True}).status_code
            == 200
        )
        assert first.post("/api/v1/datasets/ds-fault-v3/sample-download").status_code == 200
    with TestClient(create_app(settings)) as second:
        item = second.get("/api/v1/datasets/rankings?metric=downloads&limit=1").json()["items"][0]
        assert item["id"] == "ds-fault-v3"
        assert item["downloadCount"] == item["favoriteCount"] == 1
        # 误加的模型统计入口已撤回，原模型目录依然可用。
        assert second.get("/api/v1/models").status_code == 200
        assert second.get("/api/v1/models/rankings").status_code == 404
