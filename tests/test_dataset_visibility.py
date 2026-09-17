"""数据集开放范围回归：权限、目录隔离、持久化及并发更新均由后端落实。"""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from test_auth import auth as auth
from test_auth import csrf, login
from test_dataset_workspace import client as client

from app.modules.applications.identity import ACTORS, Actor
from app.modules.common.schemas import CatalogQuery
from app.modules.datasets.admin_schemas import DatasetVisibility
from app.modules.datasets.admin_service import DatasetAdminService
from app.modules.datasets.repository import DatasetRecord, DatasetRepository
from app.modules.datasets.workspace import DatasetWorkspace

DATASET = "ds-fault-v3"
PATH = "/api/v1/admin/datasets/" + DATASET


@pytest.mark.parametrize(
    ("scope", "targets"),
    [
        ("organizations", ["org-production"]),
        ("members", ["demo-outsider"]),
        ("projects", ["project-b"]),
    ],
)
def test_scope_filters_every_user_entry_and_can_be_restored(client, scope, targets):
    """范围保存后立即隐藏列表、详情、排行和收藏，直接请求下载也不能绕过。"""
    user_path = "/api/v1/datasets/" + DATASET
    baseline = client.get("/api/v1/datasets").json()["total"]
    assert client.put(user_path + "/favorite", json={"favorite": True}).status_code == 200
    detail = client.get(PATH).json()
    assert detail["canManageVisibility"] is True
    assert detail["visibility"] == {"scope": "all", "targets": [], "revision": 0}
    changed = client.put(
        PATH + "/visibility", json={"scope": scope, "targets": targets, "revision": 0}
    )
    assert changed.status_code == 200
    assert changed.json()["revision"] == 1
    assert client.get(PATH).json()["visibility"] == changed.json()
    assert client.get("/api/v1/datasets").json()["total"] == baseline - 1
    assert client.get(user_path).status_code == 404
    assert client.get("/api/v1/dataset-activity").json()["favoriteIds"] == []
    assert DATASET not in {
        item["id"] for item in client.get("/api/v1/datasets/rankings?limit=100").json()["items"]
    }
    assert client.put(user_path + "/favorite", json={"favorite": False}).status_code == 404
    assert client.post(user_path + "/sample-download").status_code == 404
    assert (
        client.post("/api/v1/resources/" + DATASET + "/invoke", json={"prompt": "测试"}).status_code
        == 404
    )
    assert client.get(PATH).json()["downloadCount"] == 0
    # 可信身份切到目标用户后可读取和下载，但普通响应不泄露配置的目标账号列表。
    client.post(
        "/api/v1/application-demo-session", json={"actorId": "demo-outsider", "audience": "user"}
    )
    allowed = client.get(user_path)
    assert allowed.status_code == 200 and "visibility" not in allowed.json()
    assert client.post(user_path + "/sample-download").status_code == 200
    assert client.put(PATH + "/visibility", json={"scope": "all", "revision": 0}).status_code == 409
    restored = client.put(
        PATH + "/visibility", json={"scope": "all", "targets": targets, "revision": 1}
    )
    assert restored.json() == {"scope": "all", "targets": [], "revision": 2}
    client.post(
        "/api/v1/application-demo-session", json={"actorId": "demo-publisher", "audience": "user"}
    )
    assert client.get("/api/v1/dataset-activity").json()["favoriteIds"] == [DATASET]


@pytest.mark.parametrize(
    "payload",
    [
        {"scope": "members", "targets": [], "revision": 0},
        {"scope": "members", "targets": [" "], "revision": 0},
        {"scope": "members", "targets": ["x" * 201], "revision": 0},
        {"scope": "other", "targets": [], "revision": 0},
        {"scope": "all"},
        {"scope": "all", "revision": -1},
        {"scope": "all", "revision": True},
        {"scope": "all", "revision": 0, "role": "admin"},
    ],
)
def test_visibility_rejects_invalid_input(client, payload):
    """非法范围、空目标、伪造字段和缺少修订号不得落库。"""
    assert client.put(PATH + "/visibility", json=payload).status_code == 422
    assert client.get(PATH).json()["visibility"]["revision"] == 0


def test_role_and_trusted_organization_control_edits(client):
    """单位管理员不能用样例展示名称取得权限，普通用户也不能调用写入接口。"""
    for actor_id in ("demo-org-admin", "demo-reader"):
        client.post(
            "/api/v1/application-demo-session", json={"actorId": actor_id, "audience": "admin"}
        )
        if actor_id == "demo-org-admin":
            assert client.get(PATH).json()["canManageVisibility"] is False
        assert (
            client.put(PATH + "/visibility", json={"scope": "all", "revision": 0}).status_code
            == 403
        )


@pytest.mark.parametrize("database", [False, True])
def test_scope_save_is_atomic_and_persists_across_instances(tmp_path, database):
    """首次并发保存只能成功一次；数据库重开仍保留范围，不能覆盖统计或活动。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'visibility.db'}") if database else None
    if engine is not None:
        DatasetRecord.__table__.create(engine)
    workspace = DatasetWorkspace(DatasetRepository(engine))
    admin = DatasetAdminService(workspace)
    payload = DatasetVisibility(
        scope="members", targets=[" demo-reader ", "demo-reader"], revision=0
    )
    workspace.interact(DATASET, ACTORS[0], favorite=True)

    def save_once():
        try:
            return admin.visibility(DATASET, payload, ACTORS[-1])
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: save_once(), range(2)))
    assert results.count(409) == 1
    reopened = DatasetWorkspace(DatasetRepository(engine)) if engine else workspace
    assert reopened.visibility(DATASET) == {
        "scope": "members",
        "targets": ["demo-reader"],
        "revision": 1,
    }
    assert reopened.resource(DATASET)["favoriteCount"] == 1
    assert any(
        item["id"] == DATASET for item in reopened.catalog(CatalogQuery(), ACTORS[1])["items"]
    )
    with pytest.raises(HTTPException) as error:
        reopened.resource(DATASET, ACTORS[2])
    assert error.value.status_code == 404
    if engine is not None:
        engine.dispose()


def test_authenticated_scope_changes_require_csrf(auth):
    """真实认证路径要求管理员及有效 CSRF，浏览器不能借开放范围接口提升权限。"""
    client, _, iam, _ = auth
    payload = {"scope": "members", "targets": ["mock-user"], "revision": 0}
    assert client.put(PATH + "/visibility", json=payload).status_code == 401
    login(client, iam)
    assert client.put(PATH + "/visibility", json=payload, headers=csrf(client)).status_code == 403
    login(client, iam, scope="admin", subject="mock-admin")
    assert client.put(PATH + "/visibility", json=payload).status_code == 403
    assert (
        client.put(PATH + "/visibility", json=payload, headers=csrf(client, "admin")).status_code
        == 200
    )


def test_configured_directory_rejects_unknown_targets(client, tmp_path):
    """已接入可信目录时只允许选择有效目标，不接受浏览器任意填写的组织编号。"""
    directory = tmp_path / "directory.json"
    directory.write_text(
        json.dumps({"organizations": [{"id": "org-research", "name": "研发"}]}), encoding="utf-8"
    )
    client.app.state.settings.applications_identity_directory = directory
    payload = {"scope": "organizations", "targets": ["unknown"], "revision": 0}
    assert client.put(PATH + "/visibility", json=payload).status_code == 422
    payload["targets"] = ["org-research"]
    assert client.put(PATH + "/visibility", json=payload).status_code == 200


def test_missing_organization_never_grants_management():
    """组织缺失的管理员不能编辑资源，避免空组织与历史样例误匹配。"""
    admin = DatasetAdminService(DatasetWorkspace(DatasetRepository()))
    with pytest.raises(HTTPException) as error:
        admin.visibility(
            DATASET,
            DatasetVisibility(scope="all", revision=0),
            Actor("a", "管理员", "org_admin", ""),
        )
    assert error.value.status_code == 403


@pytest.mark.parametrize("scope", ["organizations", "members", "projects"])
def test_demo_directory_rejects_unknown_target_without_changing_revision(client, scope):
    """演示目录已有确定对象，拼错或跨类型编号不能保存成无人可见的范围。"""
    response = client.put(
        PATH + "/visibility",
        json={"scope": scope, "targets": ["not-in-demo-directory"], "revision": 0},
    )
    assert response.status_code == 422
    assert client.get(PATH).json()["visibility"]["revision"] == 0


def test_visibility_description_matches_default_and_saved_scope(client):
    """未配置和保存后的目录说明均由实际规则生成，不使用固定样例中的旧文案。"""
    resource = client.get(PATH).json()
    assert {item["value"] for item in resource["metadata"] if item["label"] == "开放范围"} == {
        "全部用户"
    }
    assert (
        client.put(
            PATH + "/visibility",
            json={"scope": "members", "targets": ["demo-publisher"], "revision": 0},
        ).status_code
        == 200
    )
    public_resource = client.get("/api/v1/datasets/" + DATASET).json()
    # 用户接口不返回管理字段，即使当前用户有权查看该资源。
    assert "metadata" not in public_resource
    assert "securityLevel" not in public_resource
    assert "endpoint" not in public_resource
    assert {
        item["value"] for item in client.get(PATH).json()["metadata"] if item["label"] == "开放范围"
    } == {"指定成员"}
    assert "visibility" not in public_resource


def test_empty_configured_directory_does_not_fall_back_to_demo(client, tmp_path):
    """正式目录为空仍代表已配置，不能回退演示对象或允许任意目标。"""
    directory = tmp_path / "empty-directory.json"
    directory.write_text("{}", encoding="utf-8")
    client.app.state.settings.applications_identity_directory = directory
    assert client.get(PATH).json()["visibilityDirectory"] == {
        "organizations": [],
        "members": [],
        "projects": [],
    }
    assert (
        client.put(
            PATH + "/visibility",
            json={"scope": "members", "targets": ["demo-reader"], "revision": 0},
        ).status_code
        == 422
    )
