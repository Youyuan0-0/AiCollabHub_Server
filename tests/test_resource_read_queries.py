"""资源查询的次数、范围、权限及读写一致性回归，所有数据写入隔离测试库。"""

from copy import deepcopy
from uuid import uuid4

import pytest

from app.modules.common.database_workspace import DatabaseResourceWorkspace
from app.modules.common.identity import Actor
from app.modules.common.resource_database import ResourceDatabase
from app.modules.common.resource_options import ResourceOptionCatalog
from app.modules.common.schemas import CatalogQuery
from app.modules.datasets.repository import DatasetRepository
from app.modules.datasets.workspace import DatasetWorkspace
from app.modules.knowledge.database import KnowledgeDatabase
from app.modules.knowledge.service import KnowledgeService
from app.modules.tools.workspace import ToolWorkspace
from tests.test_foundation_resource_storage import draft, foundation
from tests.test_knowledge_catalog import count_selects
from tests.test_resource_foundation import connection, pytestmark, schema_connection

__all__ = ["foundation", "connection", "schema_connection", "pytestmark"]
WRITER = Actor("writer", "测试发布者", "publisher", "org-one", ("project-one",))
ADMIN = Actor("reviewer", "测试管理员", "admin", "org-two")


def publish(repository: ResourceDatabase, name: str, *, report: bool = False) -> dict:
    """创建带审核历史的正式资源，启用后供查询契约验证。"""
    record = draft(repository.kind)
    record["organizationId"] = WRITER.organization_id
    record["data"].update(name=name, toolType="文本处理", team="研发团队")
    with repository.transaction() as unit:
        unit.put_publication(record)
        pending = unit.get_publication(record["id"])
        reports = []
        if report:
            report_id = str(uuid4())
            unit.stage_report(
                {
                    "id": report_id,
                    "name": "审核报告.pdf",
                    "size": 10,
                    "contentType": "application/pdf",
                    "createdBy": ADMIN.id,
                    "objectKey": f"reports/{report_id}.pdf",
                    "previewKey": f"reports/{report_id}.html",
                    "previewSize": 12,
                    "previewContentType": "text/html",
                    "publicationId": pending["id"],
                    "publicationRevision": pending["revision"],
                }
            )
            reports = [{"id": report_id}]
        pending.update(status="approved", revision=pending["revision"] + 1)
        pending["reviews"].append(
            {
                "id": str(uuid4()),
                "reviewerId": ADMIN.id,
                "decision": "approved",
                "reason": "符合要求",
                "scores": {"quality": 4},
                "reportFiles": reports,
            }
        )
        unit.put_publication(pending)
        resource = unit.get_resource(pending[unit.resource_key])
        resource["listingStatus"] = "listed"
        unit.save_resource_state(resource)
    return record


@pytest.mark.parametrize("kind", ["dataset", "model", "application", "knowledge", "tool"])
def test_publication_lists_batch_reports_and_keep_detail_parity(foundation, connection, kind):
    """增加带报告的申请不会线性增加 SQL；批量结果与单条详情及正式版本相同。"""
    foundation.warm_up()
    repository = ResourceDatabase(foundation, kind)
    records = [publish(repository, "首个资源", report=True)]
    with count_selects(connection) as initial:
        with repository.transaction(is_readonly=True) as unit:
            assert len(unit.list_publications()) == 1
    records.extend(publish(repository, f"资源{index}", report=True) for index in range(3))
    with repository.transaction(is_readonly=True) as unit:
        expected = {item["id"]: unit.get_publication(item["id"]) for item in records}
    with count_selects(connection) as queries:
        with repository.transaction(is_readonly=True) as unit:
            actual = unit.list_publications()
    assert len(queries) == len(initial) <= 16
    assert {item["id"]: item for item in actual} == expected
    assert all(
        item["reviews"][0]["reportFiles"][0]["previewContentType"] == "text/html" for item in actual
    )


def test_tool_catalog_reads_interactions_once_and_keeps_current_counts(foundation, connection):
    """目录查询数不随工具数量增长，分页、本人互动和成功/失败调用统计保持正确。"""
    foundation.warm_up()
    repository = ResourceDatabase(foundation, "tool")
    workspace = ToolWorkspace(repository)
    first = publish(repository, "常用工具")
    tool_id = first["toolId"]
    workspace.interact(tool_id, WRITER, favorite=True, rating=4, comment="本人评价")
    workspace.interact(tool_id, ADMIN, rating=2, comment="其他评价")
    with repository.transaction() as unit:
        for index, success in enumerate((True, False)):
            key = str(uuid4())
            unit.put_entry(
                "runs",
                key,
                {
                    "id": key,
                    "toolId": tool_id,
                    "actorId": WRITER.id,
                    "success": success,
                    "durationMs": 100 + 100 * index,
                    "createdAt": first["createdAt"],
                    "version": "1.0",
                    "mode": "simulation",
                },
            )
    with count_selects(connection) as initial:
        baseline = workspace.catalog(CatalogQuery(), WRITER)
    for index in range(3):
        publish(repository, f"更多工具{index}")
    with count_selects(connection) as queries:
        page = workspace.catalog(CatalogQuery(pageSize=1), WRITER)
    assert len(queries) == len(initial) <= 14
    assert page["total"] == 4 and page["items"] == baseline["items"]
    item = page["items"][0]
    assert item["favoriteCount"] == 1 and item["myRating"] == 4
    assert item["rating"] == 3 and item["ratingCount"] == 2
    assert item["runtime"]["successRate"] == 50 and item["runtime"]["averageResponseMs"] == 150
    assert workspace.catalog(CatalogQuery(), WRITER, favorites_only=True)["total"] == 1
    assert len(workspace.ratings(tool_id, WRITER)) == 2
    assert workspace.interact(tool_id, WRITER, favorite=False)["favoriteCount"] == 0


def test_scoped_interactions_and_write_freshness(foundation):
    """过滤条件仅返回对应账号及资源；写事务读取后修改仍看到最新状态。"""
    repository = ResourceDatabase(foundation, "tool")
    first, second = [publish(repository, name)["toolId"] for name in ("工具一", "工具二")]
    with repository.transaction() as unit:
        assert unit.get_visibility(first)["scope"] == "all"
        unit.put_visibility(first, {"scope": "members", "targets": [WRITER.id], "revision": 2})
        assert unit.get_visibility(first)["targets"] == [WRITER.id]
        for owner, resource_id in ((WRITER.id, first), (ADMIN.id, first), (WRITER.id, second)):
            unit.put_entry(
                "interactions",
                f"{owner}:{resource_id}",
                {
                    "actorId": owner,
                    "toolId": resource_id,
                    "favorite": True,
                },
            )
        mine = unit.interaction_entries(WRITER.id, first)
        assert len(mine) == 1 and mine[0]["favorite"]
        unit.put_entry("interactions", f"{WRITER.id}:{first}", {**mine[0], "favorite": False})
        assert unit.interaction_entries(WRITER.id, first) == []
    with repository.transaction(is_readonly=True) as unit:
        assert {item["actorId"] for item in unit.interaction_entries(resource_id=first)} == {
            ADMIN.id
        }
        assert {item["toolId"] for item in unit.interaction_entries(subject=WRITER.id)} == {second}


def test_dataset_detail_only_projects_selected_resource(foundation, monkeypatch):
    """单条详情不组装无关目录；申请列表也不再额外重载资源状态。"""
    repository = DatasetRepository(foundation=foundation)
    workspace = DatasetWorkspace(repository)
    records = [publish(repository.database, f"数据集{index}") for index in range(3)]
    from app.modules.common.resource_catalog import ResourceCatalog

    original = ResourceCatalog.resource_view
    projected = []

    def project(unit, row):
        projected.append(str(row["id"]))
        return original(unit, row)

    monkeypatch.setattr(ResourceCatalog, "resource_view", project)
    result = workspace.resource(records[0]["datasetId"], WRITER)
    assert projected == [result["id"]]
    publications = repository.list("publication:")
    monkeypatch.setattr(repository, "list", lambda key: pytest.fail("unexpected catalog reload"))
    assert workspace.publication_listings(publications) == publications


def test_dependency_options_match_full_catalogs_for_all_scopes(foundation, connection):
    """轻量选项与原目录权限和顺序一致，不加载文档、报告或用户互动明细。"""
    foundation.warm_up()
    repositories = {
        kind: ResourceDatabase(foundation, kind)
        for kind in ("dataset", "knowledge", "model", "tool")
    }
    for kind, repository in repositories.items():
        for scope, targets in (
            ("all", []),
            ("members", [WRITER.id]),
            ("organizations", ["org-one"]),
            ("projects", ["project-one"]),
        ):
            record = publish(repository, f"{kind}-{scope}")
            with repository.transaction() as unit:
                unit.put_visibility(
                    record[unit.resource_key],
                    {
                        "scope": scope,
                        "targets": targets,
                        "revision": 2,
                    },
                )
        disabled = publish(repository, f"{kind}-禁用")
        with repository.transaction() as unit:
            resource = unit.get_resource(disabled[unit.resource_key])
            unit.save_resource_state({**resource, "listingStatus": "unlisted"})
            unit.put_publication(draft(kind))
    datasets = DatasetWorkspace(DatasetRepository(foundation=foundation))
    knowledge = KnowledgeService(KnowledgeDatabase(repositories["knowledge"]), None, None)
    models = DatabaseResourceWorkspace(repositories["model"])
    tools = ToolWorkspace(repositories["tool"])
    actors = (
        WRITER,
        ADMIN,
        Actor("outsider", "外部用户", "user", "org-two"),
        Actor("unit-admin", "单位管理员", "org_admin", "org-one"),
        Actor("teammate", "项目成员", "user", "org-two", ("project-one",)),
    )
    for actor in actors:
        sources = {
            "dataset": datasets.resources(actor),
            "knowledge": knowledge.published_resources(actor),
            "model": models.catalog(actor),
            "tool": tools.catalog(CatalogQuery(pageSize=100), actor)["items"],
        }
        expected = [
            {
                "kind": kind,
                "resourceId": item["id"],
                "name": item["name"],
                "owner": item.get("owner", ""),
                "version": item.get("version", ""),
            }
            for kind, resources in sources.items()
            for item in resources
        ]
        with count_selects(connection) as queries:
            actual = ResourceOptionCatalog(foundation).list(actor)
        assert actual == expected
        assert len(queries) == 6
        assert not any("knowledge_documents" in sql or "resource_reviews" in sql for sql in queries)
    # 调用方修改投影不能污染下一次请求的权限或结果。
    actual[0]["name"] = "被调用方修改"
    assert ResourceOptionCatalog(foundation).list(actor) == deepcopy(expected)


def test_dependency_knowledge_uses_latest_management_scope_and_published_overview(foundation):
    """最新申请的管理单位改变时权限保持一致，选项不得带出待审名称。"""
    repository = ResourceDatabase(foundation, "knowledge")
    record = publish(repository, "正式知识库")
    resource_id = record["resourceId"]
    pending = draft("knowledge")
    pending.update(resourceId=resource_id, createdBy=ADMIN.id, organizationId="org-two")
    pending["data"]["name"] = "待审知识库"
    with repository.transaction() as unit:
        unit.put_visibility(
            resource_id, {"scope": "members", "targets": [WRITER.id], "revision": 2}
        )
        unit.put_publication(pending)
    knowledge = KnowledgeService(KnowledgeDatabase(repository), None, None)
    for organization, expected_count in (("org-one", 0), ("org-two", 1)):
        actor = Actor("unit-admin", "单位管理员", "org_admin", organization)
        options = ResourceOptionCatalog(foundation).list(actor)
        assert len(options) == len(knowledge.published_resources(actor)) == expected_count
        if options:
            assert options[0]["name"] == "正式知识库"
