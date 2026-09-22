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
from app.modules.tools.schemas import ReviewTool, SaveTool
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
        item["id"], None, ReviewInput(revision=item["revision"], decision="approved"), ADMIN
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
    reviewed = KnowledgeReviews(service).decide(
        item["id"], None, ReviewInput(revision=changed["revision"], decision="approved"), ADMIN
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
