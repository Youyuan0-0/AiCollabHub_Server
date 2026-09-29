"""知识库目录的批量查询、正式版隔离和文档权限回归，使用隔离 PostgreSQL。"""

from contextlib import contextmanager
from io import BytesIO
from uuid import uuid4

import pytest
from fakeredis import FakeRedis
from fastapi import UploadFile
from sqlalchemy import event

from app.cache.client import RedisCache
from app.modules.common.identity import Actor
from app.modules.common.resource_database import ResourceDatabase
from app.modules.common.resource_runtime import ResourceRuntime
from app.modules.common.schemas import PageQuery
from app.modules.knowledge.database import KnowledgeDatabase
from app.modules.knowledge.database_files import KnowledgeDatabaseFiles
from app.modules.knowledge.retrieval import RetrievalProvider
from app.modules.knowledge.reviews import KnowledgeReviews
from app.modules.knowledge.schemas import AdminUpdate, CreateInput, ReviewInput, VisibilityInput
from app.modules.knowledge.service import KnowledgeService
from app.storage.files import FileStore
from tests.test_foundation_module_flows import ADMIN, SCORES, WRITER, Storage
from tests.test_foundation_resource_storage import foundation
from tests.test_resource_foundation import connection, pytestmark, schema_connection

__all__ = ["foundation", "connection", "schema_connection", "pytestmark"]
READER = Actor("reader", "普通读者", "user", "other")


@pytest.fixture
def service(foundation):
    client = FakeRedis(decode_responses=True)
    runtime = ResourceRuntime(
        RedisCache(client, namespace="catalog-test", default_ttl=60), "knowledge"
    )
    repository = KnowledgeDatabase(ResourceDatabase(foundation, "knowledge", runtime=runtime))
    foundation.warm_up()
    yield KnowledgeService(
        repository,
        KnowledgeDatabaseFiles(FileStore(foundation, Storage())),
        RetrievalProvider(foundation.settings),
    )
    client.close()


def create_base(service, name, *, document_count=2):
    """创建带审核报告、预览和已审/待审文档的正式知识库。"""
    item = service.create(
        CreateInput(name=name, description="目录回归"),
        WRITER,
        {"owner": "测试单位", "contact": "联系人"},
    )
    record = service.repository.get(item["id"])
    report_id = str(uuid4())
    with service.repository.resources.transaction() as unit:
        unit.stage_report(
            {
                "id": report_id,
                "name": "审核报告.pdf",
                "size": 12,
                "contentType": "application/pdf",
                "createdBy": ADMIN.id,
                "objectKey": f"reports/{report_id}.pdf",
                "previewKey": f"reports/{report_id}.html",
                "previewSize": 15,
                "previewContentType": "text/html",
                "publicationId": record["publicationId"],
                "publicationRevision": record["reviewRevision"],
            }
        )
    item = KnowledgeReviews(service).decide(
        item["id"],
        None,
        ReviewInput(revision=item["revision"], decision="approved", scores=SCORES),
        ADMIN,
        reports=[{"id": report_id}],
    )
    item = service.repository.set_listing(item["id"], "listed", item["revision"], ADMIN)
    item = service.visibility(
        item["id"], VisibilityInput(scope="all", targets=[], revision=item["revision"]), ADMIN
    )
    item = service.upload(
        item["id"],
        [
            UploadFile(BytesIO(b"document contents"), filename=f"document-{index}.txt")
            for index in range(document_count)
        ],
        item["revision"],
        WRITER,
    )
    item = KnowledgeReviews(service).decide(
        item["id"],
        item["documents"][0]["id"],
        ReviewInput(revision=item["revision"], decision="approved"),
        ADMIN,
    )
    service.process(item["id"], item["jobId"])
    return service.repository.get(item["id"])


@contextmanager
def count_selects(connection):
    queries = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)

    event.listen(connection, "before_cursor_execute", capture)
    try:
        yield queries
    finally:
        event.remove(connection, "before_cursor_execute", capture)


def test_catalog_query_count_does_not_grow_with_resources_or_documents(
    service, connection, monkeypatch
):
    """增加知识库、文档和报告后查询次数保持不变，且完整数据与详情一致。"""
    records = [create_base(service, "知识库一")]
    cache = service.repository.resources.runtime.cache.client
    original_mget = cache.mget
    batches = []

    def mget(keys):
        batches.append(len(keys))
        return original_mget(keys)

    monkeypatch.setattr(cache, "mget", mget)
    with count_selects(connection) as first_queries:
        assert service.repository.list() == records
    assert batches == [1]
    for index in range(3):
        records.append(create_base(service, f"更多知识库{index}", document_count=5))
    batches.clear()
    with count_selects(connection) as larger_queries:
        actual = service.repository.list()
    assert len(first_queries) == len(larger_queries) <= 20
    assert batches == [4]
    assert actual == sorted(records, key=lambda record: record["id"])
    for record in actual:
        assert record == service.repository.get(record["id"])
        reports = record["reviewHistory"][0]["reportFiles"]
        assert reports[0]["previewContentType"] == "text/html"
        assert reports[0]["publicationId"] == record["publicationId"]


def test_catalog_keeps_published_content_and_filters_before_paging(service):
    """读者看到正式资料和已审文档，作者可见待审修改，筛选总数不含私有资源。"""
    public = create_base(service, "公开知识库")
    service.update(
        public["id"],
        AdminUpdate(
            name="尚未审核的名称",
            description="未公开资料",
            owner="测试单位",
            contact="联系人",
            revision=public["revision"],
        ),
        ADMIN,
    )
    private = create_base(service, "私有知识库")
    service.visibility(
        private["id"],
        VisibilityInput(scope="members", targets=[WRITER.id], revision=private["revision"]),
        ADMIN,
    )
    disabled = create_base(service, "停用知识库")
    service.repository.set_listing(disabled["id"], "unlisted", disabled["revision"], ADMIN)
    draft = service.create(
        CreateInput(name="首次待审", description="尚无主表"),
        WRITER,
        {"owner": "测试单位", "contact": "联系人"},
    )
    page = service.catalog(READER, PageQuery(page=1, page_size=1))
    assert page["total"] == 1
    item = page["items"][0]
    assert item["name"] == "公开知识库"
    assert item["documentCount"] == 1 and len(item["documents"]) == 1
    assert "review" not in item and "review" not in item["documents"][0]
    assert item == service.present(service.record(public["id"], READER), READER)
    assert service.catalog(READER, PageQuery(q="私有"))["total"] == 0
    assert service.catalog(READER, PageQuery(q="尚未审核"))["total"] == 0
    for actor, admin in [(WRITER, False), (ADMIN, True)]:
        items = service.catalog(actor, admin=admin)
        assert len(items) == 4
        assert next(row for row in items if row["id"] == public["id"])["name"] == "尚未审核的名称"
        assert any(row["id"] == draft["id"] for row in items)
    assert {item["name"] for item in service.published_resources(ADMIN)} == {
        "公开知识库",
        "私有知识库",
    }
    assert (
        service.catalog(Actor("unit-admin", "其他单位管理员", "org_admin", "other"), admin=True)
        == []
    )


def test_catalog_runtime_stamp_and_deleted_document_match_detail(service):
    """删除文档仍参与修订；过期运行态不能继续冒充有效索引。"""
    base = create_base(service, "索引知识库")
    service.delete_document(base["id"], base["documents"][0]["name"], base["revision"], WRITER)
    current = service.repository.get(base["id"])
    assert service.repository.list() == [current]
    assert current["revision"] != base["revision"]
    assert len(current["reviewHistory"]) == len(base["reviewHistory"])
    other = create_base(service, "旧运行态知识库")
    runtime = service.repository.resources.runtime
    state = runtime.get("index", other["id"])
    state["databaseStamp"] = "outdated"
    runtime.put("index", other["id"], state)
    actual = next(item for item in service.repository.list() if item["id"] == other["id"])
    assert actual["status"] == "stale" and actual["needsRebuild"]
    assert actual["chunkCount"] == 0
    assert actual == service.repository.get(other["id"])


def test_detail_batches_documents_and_keeps_legacy_snapshot(service, connection):
    """详情查询不随文档数量增长，修订、审核快照与原写流程的读取完全一致。"""
    first = create_base(service, "较少文档", document_count=2)
    larger = create_base(service, "较多文档", document_count=7)
    with count_selects(connection) as initial:
        service.record(first["id"], READER)
    with count_selects(connection) as queries:
        actual = service.record(larger["id"], READER)
    assert len(initial) == len(queries) <= 20
    with service.repository.resources.transaction(is_readonly=True) as unit:
        previous = service.repository._read(unit, larger["id"])
        expected = service.repository._public_record(previous, unit.get_resource(larger["id"]))
    assert actual == expected
    assert service.record(larger["id"].upper(), READER)["name"] == "较多文档"
