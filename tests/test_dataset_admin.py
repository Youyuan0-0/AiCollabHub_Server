"""数据集后台契约回归，覆盖审批权限、修订冲突和第三方尚未接入的边界。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from test_auth import auth as auth
from test_auth import csrf, login
from test_dataset_workspace import client as client
from test_dataset_workspace import data as data

from app.modules.common.identity import ACTORS, Actor
from app.modules.datasets.admin_schemas import DatasetAuditQuery, DatasetReviewQuery, ReviewDataset
from app.modules.datasets.admin_service import DatasetAdminService
from app.modules.datasets.publishing import ExternalDatasetPublisher
from app.modules.datasets.repository import DatasetRecord, DatasetRepository
from app.modules.datasets.schemas import DatasetInput, SaveDataset
from app.modules.datasets.workspace import DatasetWorkspace

SCORES = {
    "formatCompliance": 4,
    "safetyCompliance": 5,
    "annotationCompliance": 4,
    "structuralCompleteness": 4,
    "authenticity": 4,
    "consistency": 4,
    "cleanliness": 4,
}


def test_review_report_association_revision_and_history(client, data):
    """模拟报告须绑定本申请、本修订；退回后历史原件仍可查看且不可删除。"""
    record = client.post("/api/v1/dataset-publications", json={"data": data, "submit": True}).json()
    other = client.post(
        "/api/v1/dataset-publications",
        json={"data": {**data, "name": "另一份数据集"}, "submit": True},
    ).json()
    path = f"/api/v1/admin/dataset-publications/{record['id']}"
    upload = client.post(
        path + "/report-files?revision=1",
        files={"file": ("quality.txt", b"quality report", "text/plain")},
    )
    assert upload.status_code == 201, upload.text
    file_id = upload.json()["id"]
    payload = {
        "decision": "needs_changes",
        "revision": 1,
        "reason": "补充说明",
        "reportFileIds": [file_id],
    }
    wrong = client.post(f"/api/v1/admin/dataset-publications/{other['id']}/review", json=payload)
    assert wrong.status_code == 422
    result = client.post(path + "/review", json=payload)
    assert result.status_code == 200, result.text
    assert result.json()["reviews"][0]["reportFiles"][0]["id"] == file_id
    content_path = path + f"/report-files/{file_id}/content"
    assert client.get(content_path).content == b"quality report"
    assert (
        client.get(content_path + "?preview=true").headers["Content-Security-Policy"] == "sandbox"
    )
    assert client.delete(path + f"/report-files/{file_id}").status_code == 409
    resubmit = client.put(
        f"/api/v1/dataset-publications/{record['id']}",
        json={"data": data, "submit": True, "revision": result.json()["revision"]},
    ).json()
    assert (
        client.post(
            path + "/review", json={**payload, "revision": resubmit["revision"]}
        ).status_code
        == 422
    )
    assert client.get(content_path).status_code == 200


def test_review_resubmit_history_and_pending_sync(client, data):
    """退回后可修改重提，历史不变；通过仍不上架且不会改变既有资源统计。"""
    client.app.state.dataset_workspace.publisher = ExternalDatasetPublisher()
    catalog = client.get("/api/v1/datasets").json()
    record = client.post("/api/v1/dataset-publications", json={"data": data, "submit": True}).json()
    user_path = "/api/v1/dataset-publications/" + record["id"]
    admin_path = "/api/v1/admin/dataset-publications/" + record["id"]
    pending = client.get("/api/v1/admin/dataset-publications").json()
    assert pending["total"] == 1 and pending["counts"]["pending"] == 1
    rejected = client.post(
        admin_path + "/review",
        json={
            "decision": "needs_changes",
            "reason": "请补充覆盖范围",
            "revision": 1,
        },
    )
    assert rejected.status_code == 200
    assert client.get(user_path).json()["reviewComment"] == "请补充覆盖范围"
    assert rejected.json()["syncStatus"] == "not_ready"
    updated_data = {**data, "name": "修订后的数据集", "timeCoverage": "2026 年"}
    resubmitted = client.put(
        user_path, json={"data": updated_data, "submit": True, "revision": 2}
    ).json()
    assert resubmitted["reviews"] == rejected.json()["reviews"]
    assert resubmitted["submittedAt"] > record["submittedAt"]
    approved = client.post(
        admin_path + "/review",
        json={
            "decision": "approved",
            "reason": "资料完整",
            "revision": 3,
            "scores": SCORES,
        },
    )
    assert approved.status_code == 200
    result = approved.json()
    assert result["status"] == "approved" and result["syncStatus"] == "pending"
    assert len(result["reviews"]) == 2
    assert result["reviews"][0]["data"] == data
    assert result["reviews"][1]["data"] == updated_data
    assert result["reviews"][1]["reviewerId"] == "demo-admin"
    assert client.get(user_path).json() == result
    assert client.get("/api/v1/datasets").json() == catalog
    assert client.get("/api/v1/datasets/" + record["datasetId"]).status_code == 404
    assert client.put(user_path, json={"data": data, "revision": 4}).status_code == 409
    assert (
        client.post(
            admin_path + "/review", json={"decision": "rejected", "reason": "重复", "revision": 4}
        ).status_code
        == 409
    )
    audit = client.get("/api/v1/admin/dataset-audit", params={"q": data["name"]}).json()
    assert audit["total"] == 1 and audit["items"][0]["data"] == data
    assert (
        client.get("/api/v1/admin/dataset-audit", params={"decision": "approved"}).json()["total"]
        == 1
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"decision": "approved"},
        {"decision": "rejected", "reason": "  "},
        {"decision": "needs_changes"},
        {"decision": "approved", "scores": {**SCORES, "safetyCompliance": 0}},
        {"decision": "approved", "scores": {**SCORES, "safetyCompliance": True}},
        {"decision": "approved", "scores": SCORES, "reviewerId": "伪造审核人"},
    ],
)
def test_review_validation_does_not_mutate(client, data, payload):
    """绕过 UI 的不完整结论或伪造系统字段不能写入审核历史。"""
    record = client.post("/api/v1/dataset-publications", json={"data": data, "submit": True}).json()
    path = "/api/v1/admin/dataset-publications/" + record["id"]
    assert client.post(path + "/review", json={"revision": 1, **payload}).status_code == 422
    assert client.get(path).json() == record
    assert client.get("/api/v1/admin/dataset-audit").json()["total"] == 0


def test_queue_validation_and_draft_readonly(client, data):
    """草稿不可审核；日期范围、分页和排序非法时返回错误，超页返回空列表。"""
    record = client.post("/api/v1/dataset-publications", json={"data": data}).json()
    path = "/api/v1/admin/dataset-publications"
    assert (
        client.post(
            path + "/" + record["id"] + "/review",
            json={"decision": "approved", "scores": SCORES, "revision": 1},
        ).status_code
        == 409
    )
    assert client.get(path).json()["counts"]["draft"] == 1
    assert client.get(path, params={"status": ""}).json()["total"] == 1
    assert client.get(path, params={"status": "", "start": "2026-01-01"}).json()["total"] == 0
    for params in (
        {"page": 0},
        {"pageSize": 101},
        {"status": "published"},
        {"start": "2026-10-01", "end": "2026-01-01"},
        {"sort": "unknown"},
    ):
        assert client.get(path, params=params).status_code == 422
    assert client.get(path, params={"status": "", "page": 99}).json()["items"] == []


@pytest.mark.parametrize("database", [False, True])
def test_organization_permissions_atomic_review_and_reopen(tmp_path, data, database):
    """内存及数据库模式均隔离组织；并发审核仅成功一次，重启后保留审核快照。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'admin.db'}") if database else None
    if engine is not None:
        DatasetRecord.__table__.create(engine)
    repository = DatasetRepository(engine)
    workspace = DatasetWorkspace(repository)
    admin = DatasetAdminService(workspace)
    record = workspace.save(SaveDataset(data=DatasetInput(**data), submit=True), ACTORS[0])
    outsider = Actor("other", "其他单位管理员", "org_admin", "org-production")
    unknown = Actor("unknown", "组织未知", "org_admin", "")
    for actor in (outsider, unknown):
        assert admin.queue(DatasetReviewQuery(), actor)["total"] == 0
        assert admin.audit(DatasetAuditQuery(), actor)["total"] == 0
        with pytest.raises(HTTPException) as error:
            admin.review(
                record["id"], ReviewDataset(decision="approved", revision=1, scores=SCORES), actor
            )
        assert error.value.status_code == 404
    assert admin.queue(DatasetReviewQuery(), ACTORS[3])["total"] == 1
    with pytest.raises(HTTPException) as error:
        admin.publication(record["id"], ACTORS[1])
    assert error.value.status_code == 403
    # 强制两个审核线程读取同一修订，以覆盖仓储 CAS 而非仅顺序状态检查。
    barrier = Barrier(2)
    original_save = repository.save

    def competing_save(key, value, expected):
        barrier.wait(timeout=5)
        return original_save(key, value, expected)

    repository.save = competing_save

    def review_once():
        try:
            admin.review(
                record["id"],
                ReviewDataset(decision="approved", revision=1, scores=SCORES),
                ACTORS[3],
            )
            return 200
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: review_once(), range(2))) == [200, 409]
    reopened = DatasetAdminService(DatasetWorkspace(DatasetRepository(engine))) if engine else admin
    result = reopened.publication(record["id"], ACTORS[4])
    assert result["revision"] == 2 and len(result["reviews"]) == 1
    assert result["syncStatus"] == "local_published"
    assert reopened.workspace.resource(record["datasetId"])["id"] == record["datasetId"]
    assert reopened.audit(DatasetAuditQuery(), ACTORS[3])["total"] == 1
    if engine:
        engine.dispose()


def test_http_admin_permission_for_all_endpoints(client):
    """所有后台端点均检查管理角色，不能用普通用户的演示会话读取目录或申请。"""
    selected = client.post(
        "/api/v1/application-demo-session", json={"actorId": "demo-reader", "audience": "admin"}
    )
    assert selected.status_code == 200
    for path in (
        "/datasets",
        "/datasets/ds-fault-v3",
        "/dataset-publications",
        "/dataset-publications/missing",
        "/dataset-audit",
    ):
        assert client.get("/api/v1/admin" + path).status_code == 403
    assert (
        client.post(
            "/api/v1/admin/dataset-publications/missing/review",
            json={"decision": "rejected", "reason": "理由", "revision": 1},
        ).status_code
        == 403
    )


def test_authenticated_admin_scope_and_csrf(auth, data):
    """正式会话路径复用公共认证；用户登录不赋予管理权限，审核必须携带管理端 CSRF。"""
    client, _, iam, _ = auth
    assert client.get("/api/v1/admin/dataset-publications").status_code == 401
    login(client, iam)
    record = client.post(
        "/api/v1/dataset-publications", json={"data": data, "submit": True}, headers=csrf(client)
    ).json()
    assert client.get("/api/v1/admin/dataset-publications").status_code == 403
    user_csrf = csrf(client)
    login(client, iam, scope="admin", subject="mock-admin")
    path = "/api/v1/admin/dataset-publications/" + record["id"]
    payload = {"decision": "approved", "scores": SCORES, "revision": 1}
    assert client.get(path).status_code == 200
    assert client.post(path + "/review", json=payload).status_code == 403
    assert client.post(path + "/review", json=payload, headers=user_csrf).status_code == 403
    reviewed = client.post(path + "/review", json=payload, headers=csrf(client, "admin"))
    assert reviewed.status_code == 200
    assert reviewed.json()["reviews"][0]["reviewerId"] == "mock-admin"


def test_audit_options_scope_exact_reviewer_and_reason(data):
    """审核人选项不受分页影响且隔离组织，相似编号精确筛选，意见按历史检索。"""
    workspace = DatasetWorkspace(DatasetRepository())
    service = DatasetAdminService(workspace)
    reviewers = [
        Actor("reviewer-1", "审核员甲", "admin", "org-group"),
        Actor("reviewer-10", "审核员乙", "admin", "org-group"),
        Actor("hidden-reviewer", "其他组织审核员", "admin", "org-group"),
    ]
    other_publisher = Actor("other-publisher", "其他单位发布者", "publisher", "org-production")
    for index, reviewer in enumerate(reviewers):
        publisher = ACTORS[0] if index < 2 else other_publisher
        record = workspace.save(SaveDataset(data=DatasetInput(**data), submit=True), publisher)
        service.review(
            record["id"],
            ReviewDataset(decision="needs_changes", reason="补充脱敏依据", revision=1),
            reviewer,
        )
        # 重提后修改名称不应替换审核历史的检索资料。
        workspace.save(
            SaveDataset(data=DatasetInput(**{**data, "name": "修改后的名称"}), revision=2),
            publisher,
            record["id"],
        )
    expected_reviewers = [{"id": item.id, "name": item.name} for item in reviewers[:2]]
    page = service.audit(DatasetAuditQuery(q="脱敏依据", pageSize=1), ACTORS[3])
    assert page["total"] == 2 and len(page["items"]) == 1
    assert page["reviewers"] == expected_reviewers
    assert page["items"][0]["data"]["name"] == data["name"]
    selected = service.audit(DatasetAuditQuery(reviewer="reviewer-1"), ACTORS[3])
    assert selected["total"] == 1
    assert selected["items"][0]["reviewerId"] == "reviewer-1"
    empty = service.audit(DatasetAuditQuery(q="修改后的名称", page=99), ACTORS[3])
    assert empty["items"] == [] and empty["total"] == 0
    assert empty["reviewers"] == expected_reviewers
