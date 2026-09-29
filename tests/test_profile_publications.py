"""验证个人发布的权限、跨模块分页和有界查询，写入仅发生在回滚的测试 schema。"""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.foundation import RESOURCE_TABLES, utc_now
from app.modules.common.identity import Actor
from app.modules.profile.repository import ProfilePublicationsRepository
from app.modules.profile.router import router
from app.modules.profile.schemas import ProfileQuery
from tests.test_foundation_resource_storage import foundation
from tests.test_resource_foundation import connection, pytestmark, schema_connection

__all__ = ["foundation", "connection", "pytestmark", "schema_connection"]


@pytest.fixture
def publications(foundation, connection):
    users = foundation.table("users")
    ids = dict(connection.execute(sa.select(users.c.iam_subject, users.c.id)).all())
    now = utc_now()
    for _, prefix, _ in RESOURCE_TABLES.values():
        rows = []
        for subject, name, status in (
            ("writer", "示例%_Alpha", None),
            ("writer", "待审核申请", "pending"),
            ("reviewer", "其他人的申请", "rejected"),
        ):
            rows.append(
                {
                    "id": uuid4(),
                    "applicant_id": ids[subject],
                    "version": "1.2",
                    "data": {
                        "name": name,
                        "version": "1.2",
                        "publicationContext": {"plannedResourceId": str(uuid4())},
                    },
                    "review_status": status,
                    "submission_no": 1 if status else 0,
                    "submitted_at": now if status else None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        connection.execute(sa.insert(foundation.table(prefix + "_publications")), rows)
    # 知识库主表没有正式版本、也没有申请时不能显示为已发布。
    connection.execute(
        sa.insert(foundation.table("knowledge_bases")).values(
            id=uuid4(),
            name="未发布占位",
            summary="测试",
            knowledge_type="document",
            creator_id=ids["writer"],
        )
    )
    return ProfilePublicationsRepository(foundation)


def test_owner_filter_and_database_route(publications, foundation):
    """个人中心包含五类本人申请；管理员在个人中心也不能读取其他人的申请。"""
    app = FastAPI()
    app.state.foundation = foundation
    actor = Actor("writer", "测试发布者", "publisher", "")
    app.state.application_identity = SimpleNamespace(resolve=lambda request, audience: actor)
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as client:
        response = client.get("/api/v1/profile/publications?pageSize=100")
    assert response.status_code == 200
    result = response.json()
    assert result["total"] == 10
    assert {item["kind"] for item in result["items"]} == set(RESOURCE_TABLES)
    assert {item["status"] for item in result["items"]} == {"draft", "pending"}
    assert all(item["name"] != "其他人的申请" for item in result["items"])
    admin = publications.list_for_user("reviewer", ProfileQuery())
    assert admin["total"] == 5
    assert all(item["name"] == "其他人的申请" for item in admin["items"])


@pytest.mark.parametrize("kind", RESOURCE_TABLES)
def test_kind_status_keyword_and_detail_links(publications, kind):
    result = publications.list_for_user(
        "writer", ProfileQuery(kind=kind, status="draft", q="alpha")
    )
    assert result["total"] == 1
    item = result["items"][0]
    assert item["name"] == "示例%_Alpha"
    assert item["version"] == ("" if kind == "knowledge" else "1.2")
    if kind == "knowledge":
        assert item["href"] == f"/knowledge?resourceId={item['id']}"
    else:
        assert item["href"].endswith(f"/publications/{item['id']}")


def test_literal_search_and_stable_pagination(publications):
    assert publications.list_for_user("writer", ProfileQuery(q="%_"))["total"] == 5
    assert publications.list_for_user("writer", ProfileQuery(q="不存在"))["total"] == 0
    all_rows = publications.list_for_user("writer", ProfileQuery(page_size=100))["items"]
    pages = [
        publications.list_for_user("writer", ProfileQuery(page=page, page_size=3))
        for page in (1, 2, 3, 4)
    ]
    assert all(page["total"] == 10 for page in pages)
    assert [item for page in pages for item in page["items"]] == all_rows
    assert publications.list_for_user("writer", ProfileQuery(page=9, page_size=3)) == {
        "items": [],
        "total": 10,
        "page": 9,
        "pageSize": 3,
    }


def test_knowledge_uses_latest_owner_and_one_row_per_resource(publications, foundation, connection):
    """同一知识库多次申请只显示最新一份，不按申请人提前筛出过时资料。"""
    table = foundation.table("knowledge_base_publications")
    users = foundation.table("users")
    writer = connection.scalar(sa.select(users.c.id).where(users.c.iam_subject == "writer"))
    reviewer = connection.scalar(sa.select(users.c.id).where(users.c.iam_subject == "reviewer"))
    old = (
        connection.execute(sa.select(table).where(table.c.applicant_id == writer))
        .mappings()
        .first()
    )
    latest = dict(old)
    latest.update(
        id=uuid4(),
        applicant_id=reviewer,
        data={**old["data"], "name": "最新归属"},
        created_at=old["created_at"] + timedelta(seconds=1),
        updated_at=old["updated_at"] + timedelta(seconds=1),
    )
    connection.execute(sa.insert(table).values(**latest))
    assert publications.list_for_user("writer", ProfileQuery(kind="knowledge"))["total"] == 1
    result = publications.list_for_user("reviewer", ProfileQuery(kind="knowledge"))
    assert result["total"] == 2
    assert result["items"][0]["name"] == "最新归属"
    assert result["items"][0]["id"] == old["data"]["publicationContext"]["plannedResourceId"]


def test_query_count_is_bounded_for_many_publications(publications, foundation, connection):
    table = foundation.table("dataset_publications")
    users = foundation.table("users")
    reviewer = connection.scalar(sa.select(users.c.id).where(users.c.iam_subject == "reviewer"))
    connection.execute(
        sa.insert(table),
        [
            {"id": uuid4(), "applicant_id": reviewer, "data": {"name": "其他人的草稿"}}
            for _ in range(100)
        ],
    )
    visitor_id = uuid4()
    connection.execute(
        sa.insert(users).values(
            id=visitor_id,
            iam_subject="visitor",
            display_name="无发布用户",
            status="active",
            role="user",
        )
    )
    queries = []

    def collect(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("SELECT"):
            queries.append(statement)

    sa.event.listen(connection, "before_cursor_execute", collect)
    try:
        result = publications.list_for_user("writer", ProfileQuery(page_size=3))
        assert result["total"] == 10 and len(result["items"]) == 3
        assert len(queries) == 3
        queries.clear()
        empty = publications.list_for_user("visitor", ProfileQuery())
        assert empty["total"] == 0 and empty["items"] == []
        assert len(queries) == 2
        assert all("resource_reviews" not in statement for statement in queries)
    finally:
        sa.event.remove(connection, "before_cursor_execute", collect)
