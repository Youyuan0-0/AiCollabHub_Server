"""数据集上下架回归：目录隔离、权限、修订竞争及重新上架后的数据保留。"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from test_auth import auth as auth
from test_auth import csrf, login
from test_dataset_admin import SCORES
from test_dataset_workspace import client as client
from test_dataset_workspace import data as data

from app.modules.common.identity import ACTORS, Actor
from app.modules.datasets.admin_schemas import DatasetListing
from app.modules.datasets.admin_service import DatasetAdminService
from app.modules.datasets.repository import DatasetRecord, DatasetRepository
from app.modules.datasets.workspace import DatasetWorkspace

DATASET = "ds-fault-v3"
PATH = "/api/v1/admin/datasets/" + DATASET
USER_PATH = "/api/v1/datasets/" + DATASET


def test_unlist_blocks_user_entries_and_relist_preserves_activity(client):
    """下架先于筛选和统计生效；恢复后原收藏、下载、评分和范围均保留。"""
    baseline = client.get("/api/v1/datasets").json()["total"]
    assert client.put(USER_PATH + "/favorite", json={"favorite": True}).status_code == 200
    assert client.post(USER_PATH + "/sample-download").status_code == 200
    assert client.put(USER_PATH + "/rating", json={"rating": 4}).status_code == 200
    before = client.get(PATH).json()
    assert before["listing"] == {"status": "listed", "revision": 0}
    assert before["canManageListing"] is True
    response = client.put(PATH + "/listing", json={"status": "unlisted", "revision": 0})
    assert response.status_code == 200
    assert response.json() == {"status": "unlisted", "revision": 1}
    assert client.get("/api/v1/datasets").json()["total"] == baseline - 1
    for suffix in ("", "/rating", "/ratings"):
        assert client.get(USER_PATH + suffix).status_code == 404
    assert client.post(USER_PATH + "/sample-download").status_code == 404
    assert client.put(USER_PATH + "/favorite", json={"favorite": False}).status_code == 404
    assert client.put(USER_PATH + "/rating", json={"rating": 5}).status_code == 404
    assert (
        client.post("/api/v1/resources/" + DATASET + "/invoke", json={"prompt": "测试"}).status_code
        == 404
    )
    assert DATASET not in client.get("/api/v1/dataset-activity").json()["favoriteIds"]
    assert DATASET not in {
        item["id"] for item in client.get("/api/v1/datasets/rankings?limit=100").json()["items"]
    }
    # 管理角色访问用户端也不能绕过下架，后台仍可读取和配置资料。
    client.post(
        "/api/v1/application-demo-session", json={"actorId": "demo-admin", "audience": "user"}
    )
    assert client.get(USER_PATH).status_code == 404
    assert client.get(PATH).status_code == 200
    hidden = client.get("/api/v1/admin/datasets?listingStatus=unlisted").json()
    assert hidden["total"] == 1 and hidden["items"][0]["id"] == DATASET
    listed = client.get("/api/v1/admin/datasets?listingStatus=listed").json()
    assert listed["total"] == baseline - 1
    assert client.get("/api/v1/admin/datasets?listingStatus=other").status_code == 422
    assert (
        client.put(PATH + "/listing", json={"status": "listed", "revision": 0}).status_code == 409
    )
    assert client.put(PATH + "/visibility", json={"scope": "all", "revision": 0}).status_code == 200
    restored = client.put(PATH + "/listing", json={"status": "listed", "revision": 1})
    assert restored.json() == {"status": "listed", "revision": 2}
    after = client.get(PATH).json()
    for key in ("downloadCount", "favoriteCount", "rating", "ratingCount", "versions"):
        assert after.get(key) == before.get(key)
    client.post(
        "/api/v1/application-demo-session", json={"actorId": "demo-publisher", "audience": "user"}
    )
    assert DATASET in client.get("/api/v1/dataset-activity").json()["favoriteIds"]
    assert "listing" not in client.get(USER_PATH).json()


def test_relisting_keeps_restricted_visibility(client):
    """重新上架继续遵循原开放范围，不能把受限资源变为全员可见。"""
    assert (
        client.put(
            PATH + "/visibility",
            json={
                "scope": "organizations",
                "targets": ["org-production"],
                "revision": 0,
            },
        ).status_code
        == 200
    )
    for status, revision in (("unlisted", 0), ("listed", 1)):
        assert (
            client.put(PATH + "/listing", json={"status": status, "revision": revision}).status_code
            == 200
        )
    assert client.get(USER_PATH).status_code == 404
    client.post(
        "/api/v1/application-demo-session", json={"actorId": "demo-outsider", "audience": "user"}
    )
    assert client.get(USER_PATH).status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "unknown", "revision": 0},
        {"status": "listed"},
        {"status": "listed", "revision": -1},
        {"status": "listed", "revision": True},
        {"status": "unlisted", "revision": 0, "updatedBy": "fake"},
    ],
)
def test_invalid_listing_does_not_mutate(client, payload):
    """非法状态、修订及伪造操作人均被拒绝，不写入状态记录。"""
    assert client.put(PATH + "/listing", json=payload).status_code == 422
    assert client.get(PATH).json()["listing"]["revision"] == 0


def test_listing_permissions_and_publication_history(client, data):
    """只允许可信归属管理员调整已发布资源，补发不能绕过下架或改写审核历史。"""
    record = client.post("/api/v1/dataset-publications", json={"data": data, "submit": True}).json()
    path = "/api/v1/admin/datasets/" + record["datasetId"]
    publication_path = "/api/v1/admin/dataset-publications/" + record["id"]
    assert (
        client.put(path + "/listing", json={"status": "listed", "revision": 0}).status_code == 404
    )
    approved = client.post(
        publication_path + "/review",
        json={
            "decision": "approved",
            "revision": 1,
            "scores": SCORES,
        },
    ).json()
    for actor_id in ("demo-reader", "demo-org-admin"):
        client.post(
            "/api/v1/application-demo-session", json={"actorId": actor_id, "audience": "admin"}
        )
        assert (
            client.put(PATH + "/listing", json={"status": "unlisted", "revision": 0}).status_code
            == 403
        )
    # 可信组织相同的单位管理员可操作新发布资料，但不能管理缺失组织编号的样例。
    assert client.get(path).json()["canManageListing"] is True
    assert (
        client.put(path + "/listing", json={"status": "unlisted", "revision": 0}).status_code == 200
    )
    result = client.post(
        publication_path + "/publish", json={"revision": approved["revision"]}
    ).json()
    assert result["listingStatus"] == "unlisted"
    assert result["revision"] == approved["revision"] and result["reviews"] == approved["reviews"]
    user_path = "/api/v1/dataset-publications/" + record["id"]
    assert client.get(user_path).json()["listingStatus"] == "unlisted"
    assert client.get("/api/v1/dataset-publications").json()[0]["listingStatus"] == "unlisted"
    queue = client.get("/api/v1/admin/dataset-publications?status=approved").json()
    assert queue["items"][0]["listingStatus"] == "unlisted"


@pytest.mark.parametrize("database", [False, True])
def test_listing_conflicts_and_persistence(tmp_path, database):
    """首次写入和后续更新均只能一个竞争请求成功，数据库重开后仍保留下架。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'listing.db'}") if database else None
    if engine is not None:
        DatasetRecord.__table__.create(engine)
    workspace = DatasetWorkspace(DatasetRepository(engine))
    admin = DatasetAdminService(workspace)
    for revision in (0, 1):

        def save_once(_, revision=revision):
            try:
                return admin.listing(
                    DATASET, DatasetListing(status="unlisted", revision=revision), ACTORS[-1]
                )
            except HTTPException as error:
                return error.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save_once, range(2)))
        assert results.count(409) == 1
    reopened = DatasetWorkspace(DatasetRepository(engine)) if engine else workspace
    assert reopened.listing(DATASET) == {"status": "unlisted", "revision": 2}
    with pytest.raises(HTTPException) as error:
        reopened.resource(DATASET, ACTORS[-1])
    assert error.value.status_code == 404
    # 组织缺失或组织不匹配均不能因空字段而获得上下架权限。
    for organization in ("", "other-org"):
        with pytest.raises(HTTPException) as error:
            admin.listing(
                DATASET,
                DatasetListing(status="listed", revision=2),
                Actor("a", "管理员", "org_admin", organization),
            )
        assert error.value.status_code == 403
    if engine is not None:
        engine.dispose()


def test_listing_requires_admin_session_and_csrf(auth):
    """正式登录下仅有效管理会话及其 CSRF 可写入，用户端 CSRF 不可复用。"""
    client, _, iam, _ = auth
    payload = {"status": "unlisted", "revision": 0}
    assert client.put(PATH + "/listing", json=payload).status_code == 401
    login(client, iam)
    user_csrf = csrf(client)
    assert client.put(PATH + "/listing", json=payload, headers=user_csrf).status_code == 403
    login(client, iam, scope="admin", subject="mock-admin")
    assert client.put(PATH + "/listing", json=payload).status_code == 403
    assert client.put(PATH + "/listing", json=payload, headers=user_csrf).status_code == 403
    assert (
        client.put(PATH + "/listing", json=payload, headers=csrf(client, "admin")).status_code
        == 200
    )
