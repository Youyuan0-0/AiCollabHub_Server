"""真实模块服务在新基线上完成送审、文件归档与文档审核。"""

from contextlib import contextmanager
from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.testclient import TestClient

from app.modules.common.identity import Actor
from app.modules.common.resource_database import ResourceDatabase
from app.modules.knowledge.database import KnowledgeDatabase
from app.modules.knowledge.database_files import KnowledgeDatabaseFiles
from app.modules.knowledge.retrieval import RetrievalProvider
from app.modules.knowledge.reviews import KnowledgeReviews
from app.modules.knowledge.schemas import CreateInput, ReviewInput
from app.modules.knowledge.service import KnowledgeService
from app.modules.tools.providers import default_configuration
from app.modules.tools.schemas import ReviewTool, SaveTool, ToolCatalogQuery, ToolListing
from app.modules.tools.workspace import ToolWorkspace
from app.storage.client import ObjectInfo
from app.storage.files import FileStore
from tests.test_foundation_resource_storage import foundation
from tests.test_resource_foundation import (
    connection,
    flush_constraints,
    pytestmark,
    schema_connection,
)

__all__ = ["foundation", "connection", "schema_connection", "pytestmark"]
WRITER = Actor("writer", "测试发布者", "publisher", "")
ADMIN = Actor("reviewer", "测试管理员", "admin", "")
SCORES = {
    key: 4
    for key in (
        "formatCompliance",
        "safetyCompliance",
        "annotationCompliance",
        "structuralCompleteness",
        "authenticity",
        "consistency",
        "cleanliness",
    )
}


class Runtime:
    def __init__(self):
        self.entries = {}

    def get(self, kind, key):
        return deepcopy(self.entries.get((kind, key)))

    def get_many(self, kind, keys):
        return {key: self.get(kind, key) for key in keys}

    def put(self, kind, key, value):
        self.entries[kind, key] = deepcopy(value)

    def list(self, kind):
        return [deepcopy(value) for (name, _), value in self.entries.items() if kind == name]


class Storage:
    bucket = "foundation-tests"
    max_upload_bytes = 100 * 1024 * 1024

    def __init__(self):
        self.objects = {}

    def put_object(self, key, stream, *, length, content_type):
        self.objects[key] = stream.read()

    def remove_object(self, key):
        self.objects.pop(key, None)

    def stat_object(self, key):
        return ObjectInfo(key, len(self.objects[key]), None, "application/octet-stream")

    @contextmanager
    def open_object(self, key):
        yield SimpleNamespace(stream=lambda size: iter([self.objects[key]]))


def test_dataset_http_report_review_and_archive(foundation, connection):
    """从真实路由上传、审核和读取历史报告，覆盖持久仓储的装配契约。"""
    from app.modules.common.database_reports import DatabaseReports
    from app.modules.datasets.admin_router import router as admin_router
    from app.modules.datasets.report_router import router as report_router
    from app.modules.datasets.repository import DatasetRepository
    from app.modules.datasets.router import router
    from app.modules.datasets.workspace import DatasetWorkspace
    from tests.test_dataset_workspace import data

    app = FastAPI()
    app.state.settings = foundation.settings
    app.state.foundation = foundation
    app.state.application_identity = SimpleNamespace(
        resolve=lambda request, audience: ADMIN if audience == "admin" else WRITER
    )
    app.state.dataset_workspace = DatasetWorkspace(DatasetRepository(foundation=foundation))
    app.state.resource_reports = DatabaseReports(foundation, Storage())
    for routes in (router, admin_router, report_router):
        app.include_router(routes, prefix="/api/v1")
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/dataset-publications", json={"data": data.__wrapped__(), "submit": True}
        )
        assert created.status_code == 201, created.text
        record = created.json()
        path = "/api/v1/admin/dataset-publications/" + record["id"]
        uploaded = client.post(
            path + "/report-files",
            params={"revision": record["revision"]},
            files={"file": ("report.txt", b"review report", "text/plain")},
        )
        assert uploaded.status_code == 201, uploaded.text
        report_id = uploaded.json()["id"]
        reviewed = client.post(
            path + "/review",
            json={
                "revision": record["revision"],
                "decision": "approved",
                "scores": SCORES,
                "reportFileIds": [report_id],
            },
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["reviews"][0]["reportFiles"][0]["id"] == report_id
        assert client.get(path + f"/report-files/{report_id}/content").content == b"review report"
        assert client.delete(path + f"/report-files/{report_id}").status_code == 409
    flush_constraints(connection)


def test_knowledge_http_report_scores_and_archive(foundation, connection):
    """知识库创建审批保存七维评分，并把质量报告归档到审核历史。"""
    from app.modules.common.database_reports import DatabaseReports
    from app.modules.knowledge.report_router import router as report_router
    from app.modules.knowledge.router import admin_router, router

    repository = KnowledgeDatabase(ResourceDatabase(foundation, "knowledge", runtime=Runtime()))
    files = KnowledgeDatabaseFiles(FileStore(foundation, Storage()))
    service = KnowledgeService(repository, files, RetrievalProvider(foundation.settings))
    app = FastAPI()
    app.state.settings = foundation.settings
    app.state.foundation = foundation
    app.state.application_identity = SimpleNamespace(
        resolve=lambda request, audience: ADMIN if audience == "admin" else WRITER
    )
    app.state.knowledge_service = service
    app.state.resource_reports = DatabaseReports(foundation, Storage())
    for routes in (router, admin_router, report_router):
        app.include_router(routes, prefix="/api/v1")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/knowledge-bases",
            json={"name": "知识库报告联调", "description": "验证评分与质量报告归档"},
        )
        assert created.status_code == 201, created.text
        record = created.json()
        detail = client.get(f"/api/v1/admin/knowledge-bases/{record['id']}")
        assert detail.status_code == 200, detail.text
        record = detail.json()
        publication_id = record["publicationId"]
        report_path = f"/api/v1/admin/knowledge-publications/{publication_id}/report-files"
        uploaded = client.post(
            report_path,
            params={"revision": record["reviewRevision"]},
            files={"file": ("knowledge-report.txt", b"knowledge review report", "text/plain")},
        )
        assert uploaded.status_code == 201, uploaded.text
        report_id = uploaded.json()["id"]
        reviewed = client.post(
            f"/api/v1/admin/knowledge-bases/{record['id']}/review",
            json={
                "revision": record["revision"],
                "decision": "approved",
                "scores": SCORES,
                "reportFileIds": [report_id],
            },
        )
        assert reviewed.status_code == 200, reviewed.text
        history = reviewed.json()["reviewHistory"][-1]
        assert history["scores"] == SCORES
        assert history["reportFiles"][0]["id"] == report_id
        listed = client.get("/api/v1/admin/knowledge-bases").json()["items"][0]
        detail = client.get(f"/api/v1/admin/knowledge-bases/{record['id']}").json()
        detail.pop("visibilityDirectory")
        assert listed == detail
        content = client.get(report_path + f"/{report_id}/content")
        assert content.status_code == 200 and content.content == b"knowledge review report"

    stored = (
        connection.exec_driver_sql(
            "SELECT id, quality_scores FROM resource_reviews "
            "WHERE resource_type='knowledge' AND publication_id=%s",
            (publication_id,),
        )
        .mappings()
        .one()
    )
    assert stored["quality_scores"] == SCORES
    archived = (
        connection.exec_driver_sql(
            "SELECT owner_type, owner_id FROM resource_file_references "
            "WHERE resource_type='knowledge' AND file_id=%s",
            (report_id,),
        )
        .mappings()
        .one()
    )
    assert archived["owner_type"] == "review" and str(archived["owner_id"]) == str(stored["id"])
    flush_constraints(connection)


def test_knowledge_publication_and_document_review(foundation, connection):
    repository = KnowledgeDatabase(ResourceDatabase(foundation, "knowledge", runtime=Runtime()))
    files = KnowledgeDatabaseFiles(FileStore(foundation, Storage()))
    service = KnowledgeService(repository, files, RetrievalProvider(foundation.settings))
    item = service.create(
        CreateInput(name="测试知识库", description="测试资料"),
        WRITER,
        {"owner": "测试单位", "contact": "测试联系人"},
    )
    assert item["review"]["status"] == "pending"
    approved = KnowledgeReviews(service).decide(
        item["id"],
        None,
        ReviewInput(revision=item["revision"], decision="approved", scores=SCORES),
        ADMIN,
    )
    assert approved["review"]["status"] == "approved"
    assert approved["listingStatus"] == "unlisted"
    doc = service.upload(
        item["id"],
        [UploadFile(BytesIO(b"foundation document"), filename="document.txt")],
        approved["revision"],
        WRITER,
    )
    reviewed = KnowledgeReviews(service).decide(
        item["id"],
        doc["documents"][0]["id"],
        ReviewInput(revision=doc["revision"], decision="approved"),
        ADMIN,
    )
    assert reviewed["documents"][0]["review"]["status"] == "approved"
    assert reviewed["jobId"]
    service.process(item["id"], reviewed["jobId"])
    completed = repository.get(item["id"])
    assert completed["status"] == "ready" and completed["chunkCount"] == 1
    from app.modules.knowledge.schemas import AdminUpdate

    enabled = repository.set_listing(item["id"], "listed", completed["revision"], ADMIN)
    changed = service.update(
        item["id"],
        AdminUpdate(
            name="更新知识库",
            description="新资料",
            owner="测试单位",
            contact="测试联系人",
            revision=enabled["revision"],
        ),
        ADMIN,
    )
    assert changed["review"]["status"] == "pending"
    assert repository.public_record(changed)["name"] == "测试知识库"
    assert repository.list() == [repository.get(item["id"])]
    reviewed = KnowledgeReviews(service).decide(
        item["id"],
        None,
        ReviewInput(revision=changed["revision"], decision="approved", scores=SCORES),
        ADMIN,
    )
    assert repository.public_record(reviewed)["name"] == "更新知识库"
    flush_constraints(connection)


def test_tool_review_reopen_reads_same_author(foundation, connection):
    from tests.test_tools import tool_data

    workspace = ToolWorkspace(
        ResourceDatabase(
            foundation, "tool", configuration=default_configuration(), runtime=Runtime()
        )
    )
    record = workspace.save(
        SaveTool(data=tool_data(), submit=True), WRITER, {"owner": "测试单位", "contact": "联系人"}
    )
    record = workspace.review(
        record["id"],
        ReviewTool(revision=record["revision"], decision="approved", scores=SCORES),
        ADMIN,
    )
    reopened = workspace.publication(record["id"], ADMIN, True)
    assert reopened["owner"] == "测试单位"
    assert reopened["reviews"][0]["reviewerId"] == ADMIN.id
    flush_constraints(connection)


def test_tool_import_reopen_and_new_version(foundation, connection):
    """数据库导入只保存一次，重建仓储后重复导入和新版本仍关联同一资源。"""
    defaults = {"owner": "测试单位", "contact": "联系人"}

    def reopen():
        return ToolWorkspace(
            ResourceDatabase(foundation, "tool", configuration=default_configuration())
        )

    workspace = reopen()
    external_id = workspace.provider.list_tools()[0]["externalId"]
    imported = workspace.import_tool(external_id, WRITER, defaults)
    flush_constraints(connection)
    reopened = reopen()
    repeated = reopened.import_tool(external_id, WRITER, defaults)
    assert (repeated["id"], repeated["revision"]) == (imported["id"], imported["revision"])
    assert repeated["source"]["externalId"] == external_id
    assert len(reopened.publications(WRITER)) == 1
    pending = reopened.save(
        SaveTool(data=repeated["data"], revision=repeated["revision"], submit=True),
        WRITER,
        defaults,
        repeated["id"],
    )
    approved = reopened.review(
        pending["id"],
        ReviewTool(revision=pending["revision"], decision="approved", scores=SCORES),
        ADMIN,
    )
    flush_constraints(connection)
    next_version = reopen().new_version(approved["toolId"], WRITER)
    flush_constraints(connection)
    persisted = reopen().publication(next_version["id"], WRITER)
    assert persisted["toolId"] == approved["toolId"]
    assert persisted["data"]["version"] != approved["data"]["version"]
    assert persisted["source"]["externalId"] == external_id
    assert reopen().new_version(approved["toolId"], WRITER)["id"] == persisted["id"]
    assert connection.exec_driver_sql("SELECT count(*) FROM ai_tool_versions").scalar_one() == 1


def test_tool_ratings_reopen_from_shared_table(foundation, connection):
    """工具评分与评论复用公共评价表，重建仓储后仍可读取且修改不会新增行。"""
    from tests.test_tools import tool_data

    def reopen():
        return ToolWorkspace(
            ResourceDatabase(foundation, "tool", configuration=default_configuration())
        )

    workspace = reopen()
    record = workspace.save(
        SaveTool(data=tool_data(), submit=True), WRITER, {"owner": "测试单位", "contact": "联系人"}
    )
    workspace.review(
        record["id"],
        ReviewTool(revision=record["revision"], decision="approved", scores=SCORES),
        ADMIN,
    )
    resource = workspace.resource(record["toolId"], ADMIN, True)
    workspace.listing(
        record["toolId"], ToolListing(status="listed", revision=resource["revision"]), ADMIN
    )
    for rating in (3, 5):
        reopen().interact(record["toolId"], WRITER, rating=rating, comment="可复用的工具评价")
    flush_constraints(connection)
    rows = (
        connection.exec_driver_sql("SELECT resource_type, rating, comment FROM resource_ratings")
        .mappings()
        .all()
    )
    assert len(rows) == 1 and dict(rows[0]) == {
        "resource_type": "tool",
        "rating": 5,
        "comment": "可复用的工具评价",
    }
    reviews = reopen().ratings(record["toolId"], ADMIN)
    assert len(reviews) == 1 and reviews[0]["comment"] == "可复用的工具评价"
    assert reviews[0]["rating"] == 5 and reviews[0]["updatedAt"]
    mine = reopen().resource(record["toolId"], WRITER)
    assert mine["myRating"] == 5 and mine["ratingCount"] == 1


def test_tool_catalog_facets_use_persisted_form_fields(foundation, connection):
    """数据库快照无需额外保存派生 facets，重读后分类、来源、团队与标签均可筛选。"""
    from tests.test_tools import tool_data

    workspace = ToolWorkspace(
        ResourceDatabase(foundation, "tool", configuration=default_configuration())
    )
    record = workspace.save(
        SaveTool(data=tool_data(team="研发团队", sourceType="第三方接入"), submit=True),
        WRITER,
        {"owner": "测试单位", "contact": "联系人"},
    )
    workspace.review(
        record["id"],
        ReviewTool(revision=record["revision"], decision="approved", scores=SCORES),
        ADMIN,
    )
    resource = workspace.resource(record["toolId"], ADMIN, True)
    workspace.listing(
        record["toolId"], ToolListing(status="listed", revision=resource["revision"]), ADMIN
    )
    reopened = ToolWorkspace(ResourceDatabase(foundation, "tool"))
    page = reopened.catalog(ToolCatalogQuery(), WRITER)
    assert {group["id"]: group["options"] for group in page["filterGroups"]} == {
        "toolType": ["文本处理"],
        "tags": ["测试"],
        "sourceType": ["第三方接入"],
        "team": ["研发团队"],
    }
    assert page["items"][0]["category"] == "文本处理"
    query = ToolCatalogQuery(
        facets='{"toolType":["文本处理"],"sourceType":["第三方接入"],"team":["研发团队"]}'
    )
    assert reopened.catalog(query, WRITER)["total"] == 1
    assert (
        reopened.catalog(ToolCatalogQuery(facets='{"toolType":["图像处理"]}'), WRITER)["total"] == 0
    )
    flush_constraints(connection)


def test_tool_publication_list_reads_legacy_and_current_owner_fields(foundation, connection):
    """旧申请的资料字段与新申请的上下文字段均可列表、搜索和查看，读取不改历史。"""
    import sqlalchemy as sa

    from app.modules.tools.admin_router import router
    from tests.test_foundation_resource_storage import draft
    from tests.test_tools import tool_data

    repository = ResourceDatabase(foundation, "tool", configuration=default_configuration())
    legacy = draft("tool")
    legacy["data"] = tool_data(name="旧格式工具", owner="历史单位", contact="历史联系人")
    current = draft("tool")
    current.update(owner="当前单位", contact="当前联系人")
    current["data"] = tool_data(name="新格式工具", owner="过期资料", contact="过期联系人")
    missing = draft("tool")
    missing["data"] = tool_data(name="未记录单位工具")
    with repository.transaction() as session:
        for record in (legacy, current, missing):
            session.put_publication(record)
    flush_constraints(connection)
    table = foundation.table("ai_tool_publications")
    before = connection.execute(sa.select(table)).mappings().all()
    app = FastAPI()
    app.state.settings = foundation.settings
    app.state.application_identity = SimpleNamespace(resolve=lambda request, audience: ADMIN)
    app.state.tool_workspace = ToolWorkspace(repository)
    app.include_router(router, prefix="/api/v1")
    endpoint = "/api/v1/admin/tool-publications"
    with TestClient(app) as client:
        response = client.get(endpoint, params={"status": "", "pageSize": 1})
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 3
        assert len(response.json()["items"]) == 1
        for record, owner, contact in (
            (legacy, "历史单位", "历史联系人"),
            (current, "当前单位", "当前联系人"),
            (missing, "", ""),
        ):
            detail = client.get(f"{endpoint}/{record['id']}")
            assert detail.status_code == 200, detail.text
            assert detail.json()["owner"] == owner
            assert detail.json()["contact"] == contact
            query = owner or record["data"]["name"]
            filtered = client.get(endpoint, params={"q": query, "status": "pending"})
            assert filtered.status_code == 200, filtered.text
            assert [item["id"] for item in filtered.json()["items"]] == [record["id"]]
        assert client.get(endpoint, params={"status": "approved"}).json()["total"] == 0
        for sort in ("submitted", "updated", "priority"):
            assert client.get(endpoint, params={"sort": sort}).status_code == 200
    assert connection.execute(sa.select(table)).mappings().all() == before


def test_tool_members_visibility_and_management_actor(foundation, connection):
    """指定用户开放范围跨请求生效，拒绝未知目标和旧修订，管理留痕记录实际用户。"""
    import sqlalchemy as sa

    from app.modules.tools.admin_router import router as admin_router
    from app.modules.tools.router import router
    from app.modules.tools.schemas import ToolListing
    from tests.test_tools import tool_data

    workspace = ToolWorkspace(
        ResourceDatabase(
            foundation, "tool", configuration=default_configuration(), runtime=Runtime()
        )
    )
    record = workspace.save(
        SaveTool(data=tool_data(), submit=True), WRITER, {"owner": "单位", "contact": "联系人"}
    )
    record = workspace.review(
        record["id"],
        ReviewTool(revision=record["revision"], decision="approved", scores=SCORES),
        ADMIN,
    )
    rid = record["toolId"]
    resource = workspace.resource(rid, ADMIN, True)
    listed = workspace.listing(
        rid, ToolListing(status="listed", revision=resource["revision"]), ADMIN
    )
    assert listed["revision"] == workspace.resource(rid, ADMIN, True)["revision"]
    app = FastAPI()
    app.state.settings = foundation.settings
    app.state.foundation = foundation
    app.state.tool_workspace = workspace
    current = {"actor": WRITER}
    app.state.application_identity = SimpleNamespace(
        resolve=lambda request, audience: ADMIN if audience == "admin" else current["actor"]
    )
    for routes in (router, admin_router):
        app.include_router(routes, prefix="/api/v1")
    with TestClient(app) as client:
        url = f"/api/v1/admin/tools/{rid}/visibility"
        revision = client.get(f"/api/v1/admin/tools/{rid}").json()["visibility"]["revision"]
        payload = {"scope": "members", "targets": [WRITER.id], "revision": revision}
        assert client.put(url, json={**payload, "targets": ["missing"]}).status_code == 422
        saved = client.put(url, json=payload)
        assert saved.status_code == 200, saved.text
        assert client.put(url, json=payload).status_code == 409
        assert client.get(f"/api/v1/admin/tools/{rid}").json()["visibility"]["targets"] == [
            WRITER.id
        ]
        assert client.get("/api/v1/tools").json()["total"] == 1
        assert client.get(f"/api/v1/tools/{rid}").status_code == 200
        assert (
            client.put(f"/api/v1/tools/{rid}/favorite", json={"favorite": True}).status_code == 200
        )
        assert (
            client.post(
                f"/api/v1/tools/{rid}/invoke", json={"inputs": {"text": "测试"}}
            ).status_code
            == 200
        )
        current["actor"] = Actor(ADMIN.id, ADMIN.name, "publisher", "")
        assert client.get("/api/v1/tools").json()["total"] == 0
        assert client.get(f"/api/v1/tools/{rid}").status_code == 404
        assert (
            client.put(f"/api/v1/tools/{rid}/favorite", json={"favorite": True}).status_code == 404
        )
        assert (
            client.post(
                f"/api/v1/tools/{rid}/invoke", json={"inputs": {"text": "测试"}}
            ).status_code
            == 404
        )
        audit = client.get("/api/v1/admin/tool-audit")
        assert audit.status_code == 200, audit.text
        events = audit.json()["items"]
        listing_event = next(item for item in events if item["decision"] == "上架")
        assert listing_event["reviewer"] == ADMIN.name
        assert listing_event["name"] == "测试工具"
        assert any(item["decision"] == "approved" for item in events)
    workspace.listing(rid, ToolListing(status="unlisted", revision=listed["revision"]), ADMIN)
    flush_constraints(connection)
    logs = foundation.table("operation_logs")
    rows = connection.execute(sa.select(logs).where(logs.c.target_id == rid)).mappings().all()
    management = [row for row in rows if row["data"].get("actorName") == ADMIN.name]
    assert len(management) == 3
    assert all(row["actor_type"] == "user" for row in management)
    assert all(row["user_id"] == foundation.user_id(connection, ADMIN.id) for row in management)
    # 旧日志的摘要没有工具名称和操作者，读取时不能报错或伪造其历史身份。
    from uuid import uuid4

    from app.modules.tools.schemas import ToolAuditQuery

    connection.execute(
        sa.insert(logs).values(
            id=uuid4(),
            actor_type="system",
            action="update",
            target_type="tool",
            target_id=rid,
            data={"reason": "旧日志缺失展示字段"},
        )
    )
    legacy = next(
        item
        for item in workspace.audit(ToolAuditQuery(), ADMIN)["items"]
        if item["reason"] == "旧日志缺失展示字段"
    )
    assert legacy["name"] == "测试工具"
    assert legacy["reviewer"] == "系统／历史未记录"
    assert legacy["decision"] == "历史操作（具体结论未记录）"
    assert legacy["createdAt"].endswith("+00:00")


def test_dataset_publish_enable_rating_download_and_reopen(foundation, connection):
    """正式数据集跨仓储读取保留统计；发送成功的下载才计数。"""
    from app.modules.common.downloads import complete_download
    from app.modules.datasets.admin_schemas import DatasetListing, ReviewDataset
    from app.modules.datasets.admin_service import DatasetAdminService
    from app.modules.datasets.repository import DatasetRepository
    from app.modules.datasets.schemas import SaveDataset
    from app.modules.datasets.workspace import DatasetWorkspace
    from tests.test_dataset_workspace import data as dataset_data

    repo = DatasetRepository(foundation=foundation, runtime=Runtime())
    workspace = DatasetWorkspace(repo)
    admin = DatasetAdminService(workspace)
    record = workspace.save(SaveDataset(data=dataset_data.__wrapped__(), submit=True), WRITER)
    approved = admin.review(
        record["id"],
        ReviewDataset(revision=record["revision"], decision="approved", scores=SCORES),
        ADMIN,
    )
    rid = approved["datasetId"]
    with pytest.raises(HTTPException):
        workspace.resource(rid, WRITER)
    admin.listing(
        rid, DatasetListing(status="listed", revision=workspace.listing(rid)["revision"]), ADMIN
    )
    workspace.interact(rid, WRITER, favorite=True)
    workspace.interact(rid, WRITER, rating=5, comment="已核对样例")
    workspace.download_sample(rid, WRITER, "sample-first")
    assert workspace.resource(rid, WRITER)["downloadCount"] == 0
    complete_download(repo.database, f"{WRITER.id}:{rid}:sample-first")
    complete_download(repo.database, f"{WRITER.id}:{rid}:sample-first")
    reopened = DatasetWorkspace(DatasetRepository(foundation=foundation))
    resource = reopened.resource(rid, WRITER)
    assert (resource["favoriteCount"], resource["rating"], resource["downloadCount"]) == (1, 5, 1)
    assert reopened.rating(rid, WRITER)["rating"] == 5
    flush_constraints(connection)


def test_application_files_versions_and_reopen(foundation, connection):
    """教程、报告及正式版本引用均不可变，发布新版本前旧资料保持可读。"""
    from app.modules.applications.application_service import ApplicationService
    from app.modules.applications.configuration import default_configuration as configuration
    from app.modules.applications.providers import DemoApplicationProvider
    from app.modules.applications.publication_schemas import (
        ReviewApplicationPublication,
        SaveApplicationPublication,
    )
    from app.modules.applications.review_files import save_report
    from app.modules.applications.tutorial_files import save_file
    from tests.test_application_publications import details

    repository = ResourceDatabase(
        foundation, "application", configuration=configuration(), runtime=Runtime()
    )
    service = ApplicationService(repository, DemoApplicationProvider())
    data = details.__wrapped__(
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(test_knowledge_id="")))
    )
    data["dependencies"] = []
    storage = Storage()
    attachment = save_file(
        service, storage, WRITER, "guide.md", b"# Introduction\nHello", "internal"
    )
    data["tutorials"] = [
        {
            "title": "使用说明",
            "kind": "document",
            "source": "file",
            "attachment": {key: value for key, value in attachment.items() if key != "kind"},
        }
    ]
    record = service.save(SaveApplicationPublication(data=data, submit=True), actor=WRITER)
    report = save_report(
        service, storage, ADMIN, record["id"], record["revision"], "review.md", b"# Review\nPassed"
    )
    approved = service.review(
        record["id"],
        ReviewApplicationPublication(
            decision="approved",
            revision=record["revision"],
            scores=SCORES,
            reportFileIds=[report["id"]],
        ),
        ADMIN,
    )
    rid = approved["applicationId"]
    resource = service.admin_resource(rid, ADMIN)
    service.set_listing(rid, "listed", resource["revision"], ADMIN)
    assert service.resource(rid, WRITER)["name"] == data["name"]
    next_record = service.create_version(rid, WRITER)
    changed = {**next_record["data"], "name": "新版应用", "version": "v2.4"}
    pending = service.save(
        SaveApplicationPublication(data=changed, revision=next_record["revision"], submit=True),
        next_record["id"],
        WRITER,
    )
    assert service.resource(rid, WRITER)["name"] == data["name"]
    service.review(
        pending["id"],
        ReviewApplicationPublication(
            decision="approved", revision=pending["revision"], scores=SCORES
        ),
        ADMIN,
    )
    reopened = ApplicationService(
        ResourceDatabase(
            foundation, "application", configuration=configuration(), runtime=Runtime()
        ),
        DemoApplicationProvider(),
    )
    assert reopened.resource(rid, WRITER)["name"] == "新版应用"
    assert len(reopened.resource(rid, WRITER)["versions"]) == 2
    with repository.transaction(is_readonly=True) as unit:
        assert unit.review_files(record["id"])[0]["reviewId"]
    flush_constraints(connection)


def test_cleanup_only_removes_expired_unreferenced_files(foundation, connection):
    """清理到期暂存对象，知识文档引用的原件继续保留。"""
    from datetime import timedelta

    from sqlalchemy import select, update

    from app.db.foundation import utc_now
    from app.storage.maintenance import cleanup_files

    storage = Storage()
    files = FileStore(foundation, storage)
    with foundation.transaction(is_readonly=True) as unit:
        writer_id = foundation.user_id(unit, WRITER.id)
    row = files.save(
        writer_id, "test", "unused.txt", BytesIO(b"temporary"), maximum=1000, extensions={".txt"}
    )
    table = foundation.table("files")
    with foundation.transaction() as unit:
        unit.execute(
            update(table)
            .where(table.c.id == row["id"])
            .values(expires_at=utc_now() - timedelta(seconds=1))
        )
    assert cleanup_files(foundation, storage) == {"deleted": 1, "deferred": 0}
    assert not storage.objects
    assert connection.scalar(select(table.c.status).where(table.c.id == row["id"])) == "deleted"
    assert cleanup_files(foundation, storage)["deleted"] == 0
    flush_constraints(connection)
