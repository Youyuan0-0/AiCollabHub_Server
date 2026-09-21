"""用户评分回归：身份隔离、真实汇总、重复更新、权限与持久化。"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from test_auth import auth as auth
from test_auth import csrf, login
from test_dataset_workspace import client as client

from app.modules.common.identity import ACTORS
from app.modules.datasets.repository import DatasetRecord, DatasetRepository
from app.modules.datasets.workspace import DatasetWorkspace

DATASET = "ds-fault-v3"
PATH = "/api/v1/datasets/" + DATASET


def test_rating_http_averages_updates_and_identity_isolation(client):
    """每个账号仅保留一份评分，汇总同时反映在卡片和详情；初始无模拟均分。"""
    assert client.get(PATH + "/rating").json() == {
        "rating": None,
        "comment": "",
        "ratingAverage": None,
        "ratingCount": 0,
    }
    assert client.get(PATH).json()["rating"] is None
    for _ in range(2):
        assert client.put(PATH + "/rating", json={"rating": 5}).json() == {
            "rating": 5,
            "comment": "",
            "ratingAverage": 5.0,
            "ratingCount": 1,
        }
    client.post(
        "/api/v1/application-demo-session", json={"actorId": "demo-reader", "audience": "user"}
    )
    assert client.get(PATH + "/rating").json()["rating"] is None
    assert client.put(PATH + "/rating", json={"rating": 2}).json()["ratingAverage"] == 3.5
    assert client.put(PATH + "/rating", json={"rating": 3}).json() == {
        "rating": 3,
        "comment": "",
        "ratingAverage": 4.0,
        "ratingCount": 2,
    }
    detail = client.get(PATH).json()
    assert detail["rating"] == 4 and detail["ratingCount"] == 2
    assert detail["qualityScore"] is None
    assert detail["downloadCount"] == 0 and detail["favoriteCount"] == 0
    card = next(
        item for item in client.get("/api/v1/datasets").json()["items"] if item["id"] == DATASET
    )
    assert card["rating"] == 4 and card["ratingCount"] == 2


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"rating": 0},
        {"rating": 6},
        {"rating": 2.5},
        {"rating": True},
        {"rating": "4"},
        {"rating": 4, "userId": "someone"},
    ],
)
def test_rating_rejects_invalid_or_forged_fields(client, payload):
    """不能绕过星级校验或冒用他人身份，拒绝后不生成评分记录。"""
    assert client.put(PATH + "/rating", json=payload).status_code == 422
    assert client.get(PATH + "/rating").json()["ratingCount"] == 0


def test_rating_checks_visibility_and_missing_resources(client):
    """撤销访问后读取和保存评分均被拒绝，已有评分不被无权请求覆盖。"""
    assert client.put(PATH + "/rating", json={"rating": 4}).status_code == 200
    assert (
        client.put(
            "/api/v1/admin/datasets/" + DATASET + "/visibility",
            json={
                "scope": "members",
                "targets": ["demo-reader"],
                "revision": 0,
            },
        ).status_code
        == 200
    )
    assert client.get(PATH + "/rating").status_code == 404
    assert client.put(PATH + "/rating", json={"rating": 1}).status_code == 404
    assert client.get("/api/v1/datasets/missing/rating").status_code == 404
    assert client.put("/api/v1/datasets/missing/rating", json={"rating": 4}).status_code == 404


def test_rating_requires_login_and_csrf(auth):
    """真实认证模式复用平台登录及 CSRF 防护，不能匿名评分。"""
    client, _, iam, _ = auth
    assert client.get(PATH + "/rating").status_code == 401
    assert client.put(PATH + "/rating", json={"rating": 4}).status_code == 401
    login(client, iam)
    assert client.put(PATH + "/rating", json={"rating": 4}).status_code == 403
    assert client.put(PATH + "/rating", json={"rating": 4}, headers=csrf(client)).status_code == 200


@pytest.mark.parametrize("database", [False, True])
def test_rating_coexists_with_activity_and_survives_reopen(tmp_path, database):
    """并发评分和收藏不覆盖各自字段，数据库重建工作区后仍返回已保存评分。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'ratings.db'}") if database else None
    if engine:
        DatasetRecord.__table__.create(engine)
    workspace = DatasetWorkspace(DatasetRepository(engine))
    actor = ACTORS[0]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(workspace.interact, DATASET, actor, rating=4),
            executor.submit(workspace.interact, DATASET, actor, favorite=True),
        ]
        for future in futures:
            future.result()
    workspace.download_sample(DATASET, actor)
    if engine:
        workspace = DatasetWorkspace(DatasetRepository(engine))
    assert workspace.rating(DATASET, actor) == {
        "rating": 4,
        "comment": "",
        "ratingAverage": 4.0,
        "ratingCount": 1,
    }
    resource = workspace.resource(DATASET, actor)
    assert resource["favoriteCount"] == 1 and resource["downloadCount"] == 1
    if engine:
        engine.dispose()


def test_rating_comments_replace_and_remain_private_after_scope_revocation(client):
    """文字评价与星级一起更新，展示原文而非 HTML，列表不暴露账号且受资源权限约束。"""
    assert client.get(PATH + "/ratings").json() == []
    text = "<script>alert(1)</script> 数据完整"
    saved = client.put(PATH + "/rating", json={"rating": 5, "comment": text})
    assert saved.status_code == 200 and saved.json()["comment"] == text
    reviews = client.get(PATH + "/ratings").json()
    assert len(reviews) == 1
    assert set(reviews[0]) == {"id", "rating", "comment", "updatedAt"}
    assert reviews[0]["updatedAt"] and reviews[0]["comment"] == text
    assert "demo-publisher" not in reviews[0]["id"]
    identity = reviews[0]["id"]
    assert (
        client.put(PATH + "/rating", json={"rating": 3, "comment": "更新评价"}).status_code == 200
    )
    updated = client.get(PATH + "/ratings").json()
    assert len(updated) == 1 and updated[0]["id"] == identity
    assert updated[0]["rating"] == 3 and updated[0]["comment"] == "更新评价"
    assert (
        client.put(PATH + "/rating", json={"rating": 3, "comment": "长" * 1001}).status_code == 422
    )
    assert client.get(PATH + "/rating").json()["comment"] == "更新评价"
    assert (
        client.put(
            "/api/v1/admin/datasets/" + DATASET + "/visibility",
            json={
                "scope": "members",
                "targets": ["demo-reader"],
                "revision": 0,
            },
        ).status_code
        == 200
    )
    assert client.get(PATH + "/ratings").status_code == 404
