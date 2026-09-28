"""知识库需求回归：真实内容、权限隔离、配置失效、并发、持久化及失败路径。"""

from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from test_auth import auth as auth
from test_auth import csrf, login

from app.core.config import Settings
from app.main import create_app
from app.modules.common.identity import ACTORS
from app.modules.common.review_schemas import ReviewQualityScores
from app.modules.knowledge.documents import parse_document, split_text
from app.modules.knowledge.repository import KnowledgeRecord, KnowledgeRepository
from app.modules.knowledge.retrieval import RetrievalProvider
from app.modules.knowledge.schemas import CreateInput, IndexInput
from app.modules.knowledge.service import KnowledgeService
from app.modules.knowledge.storage import KnowledgeFiles

ROOT = "/api/v1/knowledge-bases"


def knowledge_scores():
    """返回知识库创建审批要求的完整七维评分。"""
    return ReviewQualityScores(**{key: 4 for key in ReviewQualityScores.model_fields}).model_dump(
        by_alias=True
    )


@pytest.fixture
def client():
    with TestClient(create_app(Settings())) as value:
        yield value


def create(client, **changes):
    """索引回归用例通过真实审批端点准备已批准知识库。"""
    response = client.post(ROOT, json={"name": "涡轮知识库", "description": "试验规范", **changes})
    assert response.status_code == 201, response.text
    base = response.json()
    approved = client.post(
        "/api/v1/admin/knowledge-bases/" + base["id"] + "/review",
        json={
            "decision": "approved",
            "revision": base["revision"],
            "scores": knowledge_scores(),
        },
    )
    assert approved.status_code == 200, approved.text
    return client.get(ROOT + "/" + base["id"]).json()


def upload(client, base, name="guide.txt", content="涡轮叶片冷却与高温材料设计规范。"):
    response = client.post(
        ROOT + "/" + base["id"] + "/documents",
        data={"revision": base["revision"]},
        files={"files": (name, content.encode())},
    )
    assert response.status_code == 202, response.text
    # 上传后尚未索引，批准本次新文档后才等待后台任务完成。
    submitted = response.json()
    document = next(doc for doc in submitted["documents"] if doc["name"] == name)
    approved = client.post(
        f"/api/v1/admin/knowledge-bases/{base['id']}/documents/{document['id']}/review",
        json={"decision": "approved", "revision": submitted["revision"]},
    )
    assert approved.status_code == 200, approved.text
    return client.get(ROOT + "/" + base["id"]).json()


def select_actor(client, actor, audience="user"):
    assert (
        client.post(
            "/api/v1/application-demo-session", json={"actorId": actor, "audience": audience}
        ).status_code
        == 200
    )


def test_real_upload_retrieval_delete_and_statistics(client):
    base = create(client)
    path = ROOT + "/" + base["id"]
    # 创建回执必须对应真实目录记录，两端都应立即读到同一知识库，防止退回仅返回模拟回执。
    assert any(item["id"] == base["id"] for item in client.get(ROOT).json())
    admin_catalog = client.get("/api/v1/admin/knowledge-bases")
    assert admin_catalog.status_code == 200
    assert any(item["id"] == base["id"] for item in admin_catalog.json()["items"])
    assert not {"dataLevel", "contact", "visibility", "createdBy", "versions"}.intersection(base)
    assert base["storageBytes"] == 0 and base["canEdit"]
    base = upload(client, base)
    assert base["status"] == "ready" and base["documentCount"] == base["chunkCount"] == 1
    assert base["storageBytes"] == len("涡轮叶片冷却与高温材料设计规范。".encode())
    chunks = client.get(path + "/chunks").json()
    assert chunks["items"][0]["documentName"] == "guide.txt"
    assert "vector" not in chunks["items"][0]
    found = client.post(
        path + "/retrieval", json={"query": "涡轮叶片", "config": base["indexConfig"]}
    )
    assert found.status_code == 200 and found.json()[0]["excerpt"] == chunks["items"][0]["text"]
    assert found.json()[0]["score"] > 0
    assert (
        client.post(
            path + "/retrieval", json={"query": "abcxyz", "config": base["indexConfig"]}
        ).json()
        == []
    )
    admin = client.get("/api/v1/admin/knowledge-bases/" + base["id"]).json()
    assert admin["retrievalCount"] == 2 and admin["retrievalSuccessRate"] == 1
    assert admin["averageRetrievalMs"] >= 0 and admin["indexBuildMs"] >= 0
    assert (
        client.delete(
            path + "/documents", params={"name": "guide.txt", "revision": base["revision"]}
        ).status_code
        == 409
    )
    deleted = client.delete(
        path + "/documents", params={"name": "guide.txt", "revision": admin["revision"]}
    ).json()
    assert deleted["documentCount"] == deleted["chunkCount"] == deleted["storageBytes"] == 0
    assert client.get(path + "/chunks").json()["total"] == 0
    assert (
        client.post(
            path + "/retrieval", json={"query": "涡轮叶片", "config": base["indexConfig"]}
        ).json()
        == []
    )


@pytest.mark.parametrize("database", [False, True])
def test_incremental_upload_retry_and_full_rebuild(client, monkeypatch, tmp_path, database):
    """内存与数据库均复用成功向量；只重试失败文件，完整重建才重新调用全部模型。"""
    service = client.app.state.knowledge_service
    engine = create_engine(f"sqlite:///{tmp_path / 'incremental.db'}") if database else None
    if engine:
        KnowledgeRecord.__table__.create(engine)
        service.repository = KnowledgeRepository(engine)
    embedded = []
    unavailable = {"故障"}

    def embed(texts, model):
        embedded.extend(texts)
        if any(word in text for text in texts for word in unavailable):
            raise HTTPException(503, "测试模型暂不可用")
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(service.provider, "embed", embed)
    config = {**IndexInput().model_dump(by_alias=True), "mode": "高质量模式"}
    try:
        base = upload(client, create(client, indexConfig=config), content="涡轮冷却规范")
        first_chunks = service.repository.get(base["id"])["chunks"]
        base = upload(client, base, name="failure.txt", content="故障分析规范")
        assert embedded == ["涡轮冷却规范", "故障分析规范"]
        assert base["indexProgress"] == {"total": 2, "ready": 1, "failed": 1, "pending": 0}
        assert service.repository.get(base["id"])["chunks"] == first_chunks
        unavailable.clear()
        path = ROOT + "/" + base["id"]
        retry = client.post(
            path + "/rebuild", json={"revision": base["revision"], "failedOnly": True}
        )
        assert retry.status_code == 202
        assert retry.json()["indexProgress"] == {"total": 2, "ready": 1, "failed": 0, "pending": 1}
        base = client.get(path).json()
        assert base["status"] == "ready" and base["chunkCount"] == 2
        assert embedded == ["涡轮冷却规范", "故障分析规范", "故障分析规范"]
        assert (
            client.post(
                path + "/rebuild", json={"revision": base["revision"], "failedOnly": True}
            ).status_code
            == 409
        )
        assert (
            client.post(path + "/rebuild", json={"revision": base["revision"]}).status_code == 202
        )
        assert embedded[-2:] == ["涡轮冷却规范", "故障分析规范"]
        base = client.get(path).json()
        changed = client.put(
            path + "/settings", json={**config, "chunkSize": 100, "revision": base["revision"]}
        ).json()
        assert changed["status"] == "stale"
        # 配置已失效时不能复用旧索引；追加文件应重新构建全部未就绪文档。
        base = upload(client, changed, name="new.txt", content="新增材料规范")
        assert embedded[-3:] == ["涡轮冷却规范", "故障分析规范", "新增材料规范"]
        assert base["indexProgress"] == {"total": 3, "ready": 3, "failed": 0, "pending": 0}
    finally:
        if engine:
            engine.dispose()


def test_retry_requires_revision_permission_and_csrf(client, auth):
    """重试仍是写操作，不能绕过修订冲突、资源权限或 CSRF。"""
    base = upload(client, create(client), name="broken.pdf", content="不是 PDF")
    path = ROOT + "/" + base["id"] + "/rebuild"
    assert client.post(path, json={"revision": 1, "failedOnly": True}).status_code == 409
    select_actor(client, "demo-outsider")
    assert (
        client.post(path, json={"revision": base["revision"], "failedOnly": True}).status_code
        == 404
    )
    secured, _, iam, _ = auth
    login(secured, iam)
    created = secured.post(
        ROOT, json={"name": "库", "description": "说明"}, headers=csrf(secured)
    ).json()
    assert (
        secured.post(
            ROOT + "/" + created["id"] + "/rebuild",
            json={"revision": created["revision"], "failedOnly": True},
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "scope,targets",
    [
        ("organizations", ["org-production"]),
        ("projects", ["project-b"]),
        ("members", ["demo-outsider"]),
    ],
)
def test_visibility_protects_all_endpoints_and_admin_organization(client, scope, targets):
    base = upload(client, create(client))
    path = ROOT + "/" + base["id"]
    admin_path = "/api/v1/admin/knowledge-bases/" + base["id"]
    select_actor(client, "demo-outsider")
    assert client.get(ROOT).json() == []
    for suffix in ("", "/chunks"):
        assert client.get(path + suffix).status_code == 404
    assert (
        client.post(
            path + "/retrieval", json={"query": "涡轮", "config": base["indexConfig"]}
        ).status_code
        == 404
    )
    changed = client.put(
        admin_path + "/visibility",
        json={"scope": scope, "targets": targets, "revision": base["revision"]},
    )
    assert changed.status_code == 200, changed.text
    visible = client.get(path).json()
    assert visible["canEdit"] is False and "visibility" not in visible
    assert client.get(path + "/chunks").json()["total"] == 1
    assert (
        client.delete(
            path + "/documents", params={"name": "guide.txt", "revision": visible["revision"]}
        ).status_code
        == 403
    )
    assert client.post(ROOT, json={"name": "越权", "description": "普通用户"}).status_code == 403
    select_actor(client, "demo-outsider", "admin")
    assert client.get("/api/v1/admin/knowledge-bases").status_code == 403
    select_actor(client, "demo-org-admin", "admin")
    assert client.get(admin_path).status_code == 200
    assert (
        client.put(
            admin_path + "/visibility",
            json={
                "scope": "organizations",
                "targets": ["org-production"],
                "revision": visible["revision"],
            },
        ).status_code
        == 422
    )


def test_settings_invalidate_index_and_rebuild_restores_real_text(client):
    base = upload(client, create(client), content="试验规范与材料性能。" * 70)
    path = ROOT + "/" + base["id"]
    previous = client.get(path + "/chunks").json()["total"]
    config = {**base["indexConfig"], "chunkSize": 100, "chunkOverlap": 10}
    stale = client.put(path + "/settings", json={**config, "revision": base["revision"]}).json()
    assert stale["status"] == "stale" and stale["needsRebuild"] and stale["chunkCount"] == 0
    assert (
        client.post(path + "/retrieval", json={"query": "材料", "config": config}).status_code
        == 409
    )
    assert client.post(path + "/rebuild", json={"revision": stale["revision"]}).status_code == 202
    ready = client.get(path).json()
    assert ready["status"] == "ready" and ready["chunkCount"] > previous
    page = client.get(path + "/chunks", params={"page": 2, "pageSize": 2}).json()
    assert len(page["items"]) == 2 and all(len(chunk["text"]) <= 100 for chunk in page["items"])
    assert (
        client.post(
            path + "/retrieval", json={"query": "材料", "config": base["indexConfig"]}
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    "change",
    [
        {"chunkSize": 99},
        {"chunkSize": 2001},
        {"chunkOverlap": 501},
        {"topK": 21},
        {"topK": True},
        {"similarityThreshold": 1.1},
        {"chunkSize": 100, "chunkOverlap": 100},
        {"mode": "未知"},
        {"keywordSearch": False},
    ],
)
def test_invalid_index_config_is_rejected_without_mutation(client, change):
    base = create(client)
    response = client.put(
        ROOT + "/" + base["id"] + "/settings",
        json={**base["indexConfig"], **change, "revision": base["revision"]},
    )
    assert response.status_code == 422
    assert client.get(ROOT + "/" + base["id"]).json() == base


def test_failed_parsing_and_missing_model_never_report_success(client):
    base = upload(client, create(client), name="broken.pdf", content="not a PDF")
    assert base["status"] == "failed" and base["chunkCount"] == 0
    assert base["documents"][0]["error"]
    config = {
        **IndexInput().model_dump(by_alias=True),
        "mode": "高质量模式",
        "retrievalMode": "向量检索",
        "semanticSearch": True,
        "keywordSearch": False,
    }
    base = upload(client, create(client, indexConfig=config))
    assert base["status"] == "failed" and "尚未配置" in base["documents"][0]["error"]
    assert base["chunkCount"] == 0


def test_upload_rejects_metadata_only_duplicates_empty_and_unsupported(client):
    base = create(client)
    path = ROOT + "/" + base["id"]
    assert (
        client.post(
            path + "/documents", json={"files": [{"name": "fake.pdf", "size": 100}]}
        ).status_code
        == 422
    )
    for name, body, code in [("empty.txt", b"", 422), ("script.exe", b"text", 415)]:
        assert (
            client.post(
                path + "/documents",
                data={"revision": base["revision"]},
                files={"files": (name, body)},
            ).status_code
            == code
        )
    assert client.get(path).json()["documentCount"] == 0
    base = upload(client, base)
    assert (
        client.post(
            path + "/documents",
            data={"revision": base["revision"]},
            files={"files": ("GUIDE.TXT", b"duplicate")},
        ).status_code
        == 409
    )
    assert client.get(path).json()["documentCount"] == 1


def test_admin_fields_history_search_and_user_cannot_set_scope(client):
    assert (
        client.post(
            ROOT, json={"name": "库", "description": "说明", "visibility": "集团公开"}
        ).status_code
        == 422
    )
    assert client.post(ROOT, json={"name": "  ", "description": "说明"}).status_code == 422
    base = create(client)
    path = "/api/v1/admin/knowledge-bases/" + base["id"]
    admin = client.get(path).json()
    payload = {
        key: admin[key]
        for key in (
            "name",
            "description",
            "descriptionMarkdown",
            "category",
            "owner",
            "contact",
            "dataLevel",
            "indexConfig",
            "revision",
        )
    }
    payload.update(dataLevel="重要数据", description="详细设计规范", owner="展示单位")
    saved = client.put(path, json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()["organizationId"] == "org-research"
    assert len(saved.json()["versions"]) == 3
    assert client.put(path, json=payload).status_code == 409
    assert client.get("/api/v1/admin/knowledge-bases?q=详细设计&pageSize=1").json()["total"] == 1
    assert "dataLevel" not in client.get(ROOT + "/" + base["id"]).json()


def test_admin_required_metadata_and_visibility_lifecycle(client):
    """后台责任信息必填；范围切换真正收回访问且保留版本，旧修订不得覆盖。"""
    root = "/api/v1/admin/knowledge-bases"
    payload = {
        "name": "权限验证库",
        "description": "测试范围",
        "owner": "研发单位",
        "contact": "负责人",
    }
    for field in ("owner", "contact"):
        for invalid in ("", "  "):
            assert client.post(root, json={**payload, field: invalid}).status_code == 422
        assert (
            client.post(root, json={k: v for k, v in payload.items() if k != field}).status_code
            == 422
        )
    created = client.post(root, json=payload)
    assert created.status_code == 201, created.text
    base = created.json()
    path = root + "/" + base["id"]
    base = client.post(
        path + "/review",
        json={
            "decision": "approved",
            "revision": base["revision"],
            "scores": knowledge_scores(),
        },
    ).json()
    public_path = ROOT + "/" + base["id"]
    assert base["canManageVisibility"] and base["contact"] == "负责人"
    assert base["visibility"]["scope"] == "members"
    select_actor(client, "demo-outsider")
    assert client.get(public_path).status_code == 404
    # 全集团范围自动清理残留目标；权限管理不能改写责任单位和数据分级。
    opened = client.put(
        path + "/visibility",
        json={
            "scope": "all",
            "targets": ["demo-admin"],
            "revision": base["revision"],
        },
    )
    assert opened.status_code == 200
    saved = opened.json()
    assert saved["visibility"]["targets"] == []
    public = client.get(public_path).json()
    assert not {"visibility", "canManageVisibility", "contact", "dataLevel"}.intersection(public)
    assert public["owner"] == "研发单位"
    assert (
        client.put(
            path + "/visibility",
            json={
                "scope": "members",
                "targets": ["demo-admin"],
                "revision": base["revision"],
            },
        ).status_code
        == 409
    )
    for scope, targets in (
        ("organizations", []),
        ("projects", ["unknown"]),
        ("members", ["unknown"]),
    ):
        assert (
            client.put(
                path + "/visibility",
                json={
                    "scope": scope,
                    "targets": targets,
                    "revision": saved["revision"],
                },
            ).status_code
            == 422
        )
    closed = client.put(
        path + "/visibility",
        json={
            "scope": "projects",
            "targets": ["project-a"],
            "revision": saved["revision"],
        },
    ).json()
    assert closed["versions"][-1]["action"] == "修改开放范围"
    assert closed["owner"] == "研发单位" and closed["dataLevel"] == "一般数据"
    assert client.get(public_path).status_code == 404
    assert client.get(public_path + "/chunks").status_code == 404
    assert (
        client.post(
            public_path + "/retrieval",
            json={
                "query": "测试",
                "config": closed["indexConfig"],
            },
        ).status_code
        == 404
    )
    assert not client.get(ROOT).json()
    select_actor(client, "demo-reader")
    assert client.get(public_path).status_code == 200
    # 编辑页同样不能清空必填项；失败后仓储修订与原值不变。
    update = {**payload, "revision": closed["revision"], "contact": " "}
    assert client.put(path, json=update).status_code == 422
    assert client.get(path).json()["revision"] == closed["revision"]


def test_database_reopen_and_conflicts(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'knowledge.db'}")
    KnowledgeRecord.__table__.create(engine)
    repository = KnowledgeRepository(engine)
    service = KnowledgeService(
        repository, KnowledgeFiles(tmp_path / "originals"), RetrievalProvider(Settings())
    )
    record = service.create(
        CreateInput(name="持久库", description="重启保留"),
        ACTORS[0],
        {"owner": "研发单位", "contact": "发布者"},
    )
    reopened = KnowledgeRepository(engine)
    assert reopened.get(record["id"]) == record
    copy = reopened.get(record["id"])
    copy["name"] = "新名称"
    reopened.save(copy, record["revision"])
    with pytest.raises(HTTPException) as error:
        repository.save(record, record["revision"])
    assert error.value.status_code == 409
    assert repository.get(record["id"])["name"] == "新名称"
    engine.dispose()


def test_office_parsers_and_chinese_fixed_chunks():
    content = BytesIO()
    with ZipFile(content, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>中文知识</w:t></w:r></w:p></w:body></w:document>',
        )
    assert parse_document("sample.docx", content.getvalue()) == "中文知识"
    content = BytesIO()
    with ZipFile(content, "w") as archive:
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row><c t="inlineStr"><is><t>涡轮</t></is></c>'
            "<c><v>42</v></c></row></sheetData></worksheet>",
        )
    assert parse_document("sample.xlsx", content.getvalue()) == "涡轮\t42"
    text = "甲乙丙丁" * 80
    config = IndexInput(chunkSize=100, chunkOverlap=20, chunkMode="固定长度")
    chunks = split_text(text, config)
    assert chunks[0] == text[:100] and chunks[1] == text[80:180]
    assert chunks[0] + "".join(chunk[20:] for chunk in chunks[1:]) == text


def test_auth_and_csrf_apply_to_knowledge(auth):
    client, _, iam, _ = auth
    assert client.get(ROOT).status_code == 401
    login(client, iam)
    assert client.post(ROOT, json={"name": "库", "description": "说明"}).status_code == 403
    base = client.post(ROOT, json={"name": "库", "description": "说明"}, headers=csrf(client))
    assert base.status_code == 201, base.text
    assert client.get("/api/v1/admin/knowledge-bases").status_code == 403


def test_knowledge_migration_has_single_head_and_preserves_existing_tables(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    engine = create_engine(url)
    assert "knowledge_records" in inspect(engine).get_table_names()
    assert "dataset_records" in inspect(engine).get_table_names()
    command.downgrade(config, "20260917_merge_ds_auth")
    assert "knowledge_records" not in inspect(engine).get_table_names()
    assert "dataset_records" in inspect(engine).get_table_names()
    engine.dispose()


def test_embedding_protocol_reorders_and_rejects_invalid_vectors(monkeypatch):
    provider = RetrievalProvider(
        Settings(knowledge_embedding_url="https://model.example/v1/embeddings")
    )

    def post(client, url, **kwargs):
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.Client, "post", post)
    assert provider.embed(["first", "second"], "bge-m3") == [[1.0, 0.0], [0.0, 1.0]]
    with pytest.raises(HTTPException) as error:
        provider.embed(["wrong count"], "bge-m3")
    assert error.value.status_code == 503


def test_vector_and_rerank_use_real_chunks_and_record_failure(client, monkeypatch):
    """替换外部模型边界，验证索引向量、召回顺序和失败统计，不使用固定演示分数。"""
    provider = client.app.state.knowledge_service.provider

    def embed(texts, model):
        return [[1.0, 0.0] if "涡轮" in text else [0.0, 1.0] for text in texts]

    monkeypatch.setattr(provider, "embed", embed)
    config = {
        **IndexInput().model_dump(by_alias=True),
        "mode": "高质量模式",
        "retrievalMode": "向量检索",
        "semanticSearch": True,
        "keywordSearch": False,
        "similarityThreshold": 0.1,
    }
    base = upload(client, create(client, indexConfig=config))
    base = upload(client, base, name="finance.txt", content="财务预算与发票流程。")
    assert base["status"] == "ready"
    path = ROOT + "/" + base["id"]
    results = client.post(path + "/retrieval", json={"query": "涡轮", "config": config}).json()
    assert len(results) == 1 and results[0]["title"] == "guide.txt" and results[0]["score"] == 1

    def rerank(query, texts):
        assert query == "涡轮" and any("财务" in text for text in texts)
        return [0.8 if "财务" in text else 0.2 for text in texts]

    monkeypatch.setattr(provider, "rerank", rerank)
    results = client.post(
        path + "/retrieval",
        json={
            "query": "涡轮",
            "config": {**config, "retrievalMode": "重排序检索", "reranking": True},
        },
    ).json()
    assert results[0]["title"] == "finance.txt"

    def fail(texts, model):
        raise HTTPException(503, "模型服务不可用")

    monkeypatch.setattr(provider, "embed", fail)
    assert (
        client.post(path + "/retrieval", json={"query": "涡轮", "config": config}).status_code
        == 503
    )
    admin = client.get("/api/v1/admin/knowledge-bases/" + base["id"]).json()
    assert admin["retrievalCount"] == 3 and admin["retrievalSuccessRate"] == 2 / 3


def test_upload_size_limit_is_atomic_and_old_job_cannot_overwrite(client, monkeypatch):
    """文件超限不会留下半批资料；重复提交的旧任务不会覆盖新的修订。"""
    import app.modules.knowledge.storage as storage

    base = create(client)
    path = ROOT + "/" + base["id"]
    monkeypatch.setattr(storage, "MAX_FILE_BYTES", 4)
    response = client.post(
        path + "/documents",
        data={"revision": base["revision"]},
        files=[("files", ("first.txt", b"one")), ("files", ("second.txt", b"too large"))],
    )
    assert response.status_code == 413
    service = client.app.state.knowledge_service
    assert list(service.files.root.iterdir()) == []
    assert client.get(path).json() == base
    service.process(base["id"], "obsolete-job")
    assert client.get(path).json() == base


def test_interrupted_index_can_be_rebuilt_and_persisted_original_is_reused(client):
    """过期构建任务展示失败并允许恢复，原文仍可用于重新生成分段。"""
    base = upload(client, create(client))
    service = client.app.state.knowledge_service
    record = service.repository.get(base["id"])
    record.update(status="syncing", jobId="interrupted", jobStartedAt="2020-01-01T00:00:00+00:00")
    service.repository.save(record, record["revision"])
    path = ROOT + "/" + base["id"]
    failed = client.get(path).json()
    assert failed["status"] == "failed" and "中断" in failed["indexError"]
    assert client.post(path + "/rebuild", json={"revision": failed["revision"]}).status_code == 202
    assert client.get(path).json()["status"] == "ready"


def test_partial_interrupted_job_retries_only_unfinished_original(client, monkeypatch):
    """过期任务保留已完成分段，读取状态不写库，部分重试只重读未完成原文。"""
    base = upload(client, create(client))
    base = upload(client, base, name="later.txt", content="后续待处理资料")
    service = client.app.state.knowledge_service
    record = service.repository.get(base["id"])
    unfinished = record["documents"][1]
    unfinished.update(status="parsing", chunkCount=0)
    record["chunks"] = [
        chunk for chunk in record["chunks"] if chunk["documentId"] != unfinished["id"]
    ]
    record.update(
        status="syncing", jobId="expired", jobStartedAt="2020-01-01T00:00:00+00:00", chunkCount=1
    )
    service.repository.save(record, record["revision"])
    path = ROOT + "/" + base["id"]
    failed = client.get(path).json()
    assert failed["indexProgress"] == {"total": 2, "ready": 1, "failed": 1, "pending": 0}
    assert service.repository.get(base["id"])["documents"][1]["status"] == "parsing"
    read = service.files.read
    read_ids = []

    def read_original(identifier):
        read_ids.append(identifier)
        return read(identifier)

    monkeypatch.setattr(service.files, "read", read_original)
    assert (
        client.post(
            path + "/rebuild", json={"revision": failed["revision"], "failedOnly": True}
        ).status_code
        == 202
    )
    ready = client.get(path).json()
    assert read_ids == [unfinished["id"]]
    assert ready["status"] == "ready" and ready["chunkCount"] == 2
    service.process(base["id"], "expired")
    assert client.get(path).json() == ready


def test_unexpected_worker_failure_exposes_retryable_documents(client, monkeypatch):
    """运行异常不得留下永久解析中状态，恢复后可通过部分重试继续处理。"""
    service = client.app.state.knowledge_service
    read = service.files.read

    def broken_read(identifier):
        raise RuntimeError("测试运行中断")

    monkeypatch.setattr(service.files, "read", broken_read)
    failed = upload(client, create(client))
    assert failed["status"] == "failed"
    assert failed["documents"][0]["status"] == "failed"
    assert failed["indexProgress"]["failed"] == 1
    monkeypatch.setattr(service.files, "read", read)
    path = ROOT + "/" + failed["id"]
    assert (
        client.post(
            path + "/rebuild", json={"revision": failed["revision"], "failedOnly": True}
        ).status_code
        == 202
    )
    assert client.get(path).json()["status"] == "ready"


def test_inflight_retrieval_metrics_do_not_break_index_job(client, monkeypatch):
    """重建开始前发起的召回可在构建期间结束，其统计更新不能使索引进度覆盖或失败。"""
    service = client.app.state.knowledge_service
    base = create(client)
    read = service.files.read

    def finish_retrieval(identifier):
        service._metrics(base["id"], 12.5, True)
        return read(identifier)

    monkeypatch.setattr(service.files, "read", finish_retrieval)
    ready = upload(client, base)
    assert ready["status"] == "ready" and ready["chunkCount"] == 1
    admin = client.get("/api/v1/admin/knowledge-bases/" + base["id"]).json()
    assert admin["retrievalCount"] == 1 and admin["averageRetrievalMs"] == 12.5


def test_legacy_publication_cannot_return_fake_knowledge_receipt(client):
    """旧通用资源入口须引导到真实创建流程，不能声称已生成待审核知识库。"""
    response = client.post(
        "/api/v1/publications",
        json={"kind": "knowledge", "name": "旧入口", "summary": "说明", "submit": True},
    )
    assert response.status_code == 422
    assert client.get(ROOT).json() == []
