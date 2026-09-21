"""知识库双层审批回归：授权、退回重提、内容隔离、审计及持久化。"""

import pytest
from sqlalchemy import create_engine
from test_auth import auth as auth
from test_auth import csrf, login
from test_knowledge import ROOT, select_actor
from test_knowledge import client as client

from app.modules.knowledge.repository import KnowledgeRecord, KnowledgeRepository

ADMIN = "/api/v1/admin/knowledge-bases"


@pytest.mark.parametrize("database", [False, True])
def test_review_lifecycle_and_unapproved_content_isolation(client, tmp_path, database):
    """两种仓储执行同一审批流程；上传和重建均不能绕过审批进入检索。"""
    service = client.app.state.knowledge_service
    engine = create_engine(f"sqlite:///{tmp_path / 'review.db'}") if database else None
    if engine:
        KnowledgeRecord.__table__.create(engine)
        service.repository = KnowledgeRepository(engine)
    try:
        data = {"name": "审核知识库", "description": "待核对资料"}
        base = client.post(ROOT, json=data).json()
        path, admin = ROOT + "/" + base["id"], ADMIN + "/" + base["id"]
        assert base["review"]["status"] == "pending"
        queued = client.get(ADMIN + "/reviews").json()
        assert queued["total"] == 1 and queued["counts"]["pending"] == 1
        # 提前设置全集团也不能暴露待审批知识库。
        base = client.put(
            admin + "/visibility",
            json={"scope": "all", "targets": [], "revision": base["revision"]},
        ).json()
        select_actor(client, "demo-outsider")
        assert client.get(ROOT).json() == []
        assert client.get(path).status_code == 404
        assert client.get(path + "/chunks").status_code == 404
        select_actor(client, "demo-publisher")
        uploaded = client.post(
            path + "/documents",
            data={"revision": base["revision"]},
            files={"files": ("待审.txt", "待审涡轮资料".encode())},
        )
        assert uploaded.status_code == 202
        base = uploaded.json()
        doc = base["documents"][0]
        doc_path = admin + "/documents/" + doc["id"]
        assert doc["status"] == "awaiting_review" and base["chunkCount"] == 0
        assert client.get(doc_path + "/preview").json()["text"] == "待审涡轮资料"
        original = client.get(doc_path + "/original")
        assert original.content == "待审涡轮资料".encode()
        assert original.headers["content-disposition"].startswith("attachment;")
        assert (
            client.post(path + "/rebuild", json={"revision": base["revision"]}).status_code == 409
        )
        assert (
            client.post(
                doc_path + "/review", json={"revision": base["revision"], "decision": "approved"}
            ).status_code
            == 409
        )
        assert (
            client.post(
                admin + "/review",
                json={"revision": base["revision"], "decision": "rejected", "reason": " "},
            ).status_code
            == 422
        )
        rejected = client.post(
            admin + "/review",
            json={
                "revision": base["revision"],
                "decision": "needs_changes",
                "reason": "补充用途说明",
            },
        ).json()
        assert rejected["review"]["reason"] == "补充用途说明"
        assert (
            client.post(
                admin + "/review", json={"revision": rejected["revision"], "decision": "approved"}
            ).status_code
            == 409
        )
        resubmitted = client.post(
            path + "/resubmit",
            json={**data, "description": "补充后的完整说明", "revision": rejected["revision"]},
        )
        assert resubmitted.status_code == 200
        base = resubmitted.json()
        assert base["review"]["status"] == "pending"
        assert (
            client.post(path + "/resubmit", json={**data, "revision": base["revision"]}).status_code
            == 409
        )
        base = client.post(
            admin + "/review", json={"revision": base["revision"], "decision": "approved"}
        ).json()
        select_actor(client, "demo-outsider")
        public = client.get(path).json()
        assert public["documents"] == [] and public["documentCount"] == public["storageBytes"] == 0
        assert "review" not in public and "reviewHistory" not in public
        assert (
            client.post(
                path + "/retrieval", json={"query": "涡轮", "config": base["indexConfig"]}
            ).json()
            == []
        )
        # 召回统计会改变修订；审核必须读取最新版本后提交。
        current = client.get(admin).json()
        assert (
            client.post(
                doc_path + "/review", json={"revision": base["revision"], "decision": "approved"}
            ).status_code
            == 409
        )
        reviewed = client.post(
            doc_path + "/review", json={"revision": current["revision"], "decision": "approved"}
        )
        assert reviewed.status_code == 200
        assert client.get(path + "/chunks").json()["items"][0]["text"] == "待审涡轮资料"
        assert client.get(path).json()["documents"][0].get("review") is None
        audit = client.get(ADMIN + "/audit?kind=base&status=").json()
        assert audit["total"] == 2
        assert {row["status"] for row in audit["items"]} == {"approved", "needs_changes"}
        if engine:
            reopened = KnowledgeRepository(engine).get(base["id"])
            assert reopened["review"]["status"] == "approved"
            assert reopened["documents"][0]["review"]["status"] == "approved"
            assert len(reopened["reviewHistory"]) == 3
    finally:
        if engine:
            engine.dispose()


def test_review_authorization_and_rejected_document_audit(client):
    """越权管理者不能看原文或审批；驳回项不会因完整重建进入索引，删除后审计仍在。"""
    base = client.post(ROOT, json={"name": "权限", "description": "说明"}).json()
    admin = ADMIN + "/" + base["id"]
    path = ROOT + "/" + base["id"]
    base = client.post(
        admin + "/review", json={"revision": base["revision"], "decision": "approved"}
    ).json()
    base = client.post(
        path + "/documents",
        data={"revision": base["revision"]},
        files={"files": ("退回.txt", b"secret")},
    ).json()
    doc_path = admin + "/documents/" + base["documents"][0]["id"]
    select_actor(client, "demo-outsider", "admin")
    for suffix in ("/preview", "/original"):
        assert client.get(doc_path + suffix).status_code == 403
    assert client.get(ADMIN + "/reviews").status_code == 403
    assert (
        client.post(
            doc_path + "/review", json={"revision": base["revision"], "decision": "approved"}
        ).status_code
        == 403
    )
    select_actor(client, "demo-admin", "admin")
    rejected = client.post(
        doc_path + "/review",
        json={"revision": base["revision"], "decision": "rejected", "reason": "请替换修订文档"},
    ).json()
    assert (
        client.post(
            doc_path + "/review", json={"revision": rejected["revision"], "decision": "approved"}
        ).status_code
        == 409
    )
    assert (
        client.post(path + "/rebuild", json={"revision": rejected["revision"]}).status_code == 202
    )
    latest = client.get(path).json()
    assert latest["chunkCount"] == 0
    assert latest["documents"][0]["status"] == "awaiting_review"
    assert (
        client.delete(
            path + "/documents", params={"name": "退回.txt", "revision": latest["revision"]}
        ).status_code
        == 200
    )
    audit = client.get(ADMIN + "/audit?kind=document&status=").json()
    assert audit["total"] == 1 and audit["items"][0]["name"] == "退回.txt"
    assert client.get(doc_path + "/original").status_code == 404


def test_review_requires_admin_session_csrf(auth):
    """审批写操作复用管理端认证及 CSRF 校验，普通入口不能伪造管理操作。"""
    client, _, iam, _ = auth
    login(client, iam)
    base = client.post(
        ROOT, json={"name": "认证", "description": "说明"}, headers=csrf(client)
    ).json()
    path = ADMIN + "/" + base["id"] + "/review"
    assert (
        client.post(path, json={"revision": base["revision"], "decision": "approved"}).status_code
        == 403
    )
    login(client, iam, scope="admin", subject="mock-admin")
    assert (
        client.post(path, json={"revision": base["revision"], "decision": "approved"}).status_code
        == 403
    )
    assert (
        client.post(
            path,
            json={"revision": base["revision"], "decision": "approved"},
            headers=csrf(client, "admin"),
        ).status_code
        == 200
    )


def test_org_admin_cannot_review_other_organization_or_read_original(client):
    """单位管理员的队列、历史和原文读取都受可信组织归属约束。"""
    base = client.post(
        ADMIN,
        json={
            "name": "集团资料",
            "description": "范围隔离",
            "owner": "展示研发单位",
            "contact": "测试管理员",
        },
    ).json()
    path = ADMIN + "/" + base["id"]
    base = client.post(
        path + "/documents",
        data={"revision": base["revision"]},
        files={"files": ("private.txt", b"private")},
    ).json()
    doc_path = path + "/documents/" + base["documents"][0]["id"]
    select_actor(client, "demo-org-admin", "admin")
    assert client.get(ADMIN + "/reviews").json()["total"] == 0
    assert client.get(ADMIN + "/reviews?kind=document").json()["total"] == 0
    assert client.get(ADMIN + "/audit?status=").json()["total"] == 0
    assert client.get(doc_path + "/original").status_code == 404
    assert client.get(doc_path + "/preview").status_code == 404
    assert (
        client.post(
            path + "/review", json={"decision": "approved", "revision": base["revision"]}
        ).status_code
        == 404
    )


def test_legacy_records_remain_available_without_fabricated_reviews(client):
    """既有资料缺少审批字段时保持可用；之后新上传仍必须经过文档审批。"""
    base = client.post(ROOT, json={"name": "既有知识库", "description": "兼容旧数据"}).json()
    service = client.app.state.knowledge_service
    record = service.repository.get(base["id"])
    record.pop("review")
    record.pop("reviewHistory")
    record["visibility"] = {"scope": "all", "targets": []}
    record = service.repository.save(record, record["revision"])
    path = ROOT + "/" + base["id"]
    select_actor(client, "demo-reader")
    assert client.get(path).status_code == 200
    admin = client.get(ADMIN + "/" + base["id"]).json()
    assert admin["review"]["legacy"] is True and admin["reviewHistory"] == []
    assert client.get(ADMIN + "/reviews?status=").json()["total"] == 0
    select_actor(client, "demo-publisher")
    uploaded = client.post(
        path + "/documents",
        data={"revision": record["revision"]},
        files={"files": ("new.txt", b"new")},
    ).json()
    assert uploaded["documents"][0]["review"]["status"] == "pending"
    assert uploaded["chunkCount"] == 0
