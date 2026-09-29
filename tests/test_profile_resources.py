"""真实测试 schema 内验证个人资源列表、可见范围和独立关系取消，全部回滚。"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from app.db.foundation import RESOURCE_TABLES, utc_now
from app.modules.common.identity import Actor
from app.modules.profile.resources import ProfileResourcesRepository
from app.modules.profile.router import router
from app.modules.profile.schemas import ProfileQuery
from tests.test_foundation_resource_storage import foundation
from tests.test_resource_foundation import (
    connection,
    flush_constraints,
    insert,
    pending,
    pytestmark,
    resource,
    review,
    schema_connection,
    update,
)

__all__ = ["foundation", "connection", "pytestmark", "schema_connection"]
WRITER = Actor("writer", "测试用户", "publisher", "org-a", ("project-a",))


def publish(connection, module, owner):
    """创建符合正式发布约束的资料，版本中保存可信单位上下文。"""
    kind, name, prefix = module
    pid = pending(connection, module, owner)
    review(connection, module, pid, owner)
    rid = resource(connection, module, owner, name=f"{kind} Alpha%_", summary="独立简介")
    now = utc_now()
    vid = insert(
        connection,
        prefix + "_versions",
        **{prefix + "_id": rid},
        publication_id=pid,
        version="1.0",
        snapshot=Jsonb({"publicationContext": {"organizationId": "org-a"}}),
        publisher_id=owner,
        published_at=now,
    )
    update(
        connection,
        prefix + "_publications",
        pid,
        **{prefix + "_id": rid},
        published_version_id=vid,
        published_at=now,
        revision=3,
    )
    update(connection, name, rid, current_version_id=vid, status="enabled")
    return rid


@pytest.fixture
def personal_resources(foundation, connection):
    users = foundation.table("users")
    ids = dict(connection.execute(sa.select(users.c.iam_subject, users.c.id)).all())
    records = {}
    for kind, (name, prefix, _) in RESOURCE_TABLES.items():
        if kind == "knowledge":
            continue
        records[kind] = publish(connection, (kind, name, prefix), ids["writer"])
        insert(
            connection,
            "resource_favorites",
            resource_type=kind,
            resource_id=records[kind],
            user_id=ids["writer"],
        )
    rid = records["application"]
    insert(
        connection,
        "resource_follows",
        resource_type="application",
        resource_id=rid,
        user_id=ids["writer"],
    )
    insert(
        connection,
        "resource_ratings",
        resource_type="application",
        resource_id=rid,
        user_id=ids["writer"],
        rating=4,
    )
    insert(
        connection,
        "resource_favorites",
        resource_type="application",
        resource_id=rid,
        user_id=ids["reviewer"],
    )
    flush_constraints(connection)
    repository = ProfileResourcesRepository(foundation)
    # 本次测试只统计业务查询，不把启动期结构反射算作每次请求的查询。
    foundation.warm_up()
    return repository, records, ids


def test_personal_resource_filters_pagination_and_owner(personal_resources):
    repo, records, _ = personal_resources
    result = repo.list_for_user(WRITER, "favorites", ProfileQuery(page_size=2))
    assert result["total"] == 4 and len(result["items"]) == 2
    second = repo.list_for_user(WRITER, "favorites", ProfileQuery(page=2, page_size=2))
    assert {item["id"] for item in result["items"] + second["items"]} == {
        str(rid) for rid in records.values()
    }
    assert repo.list_for_user(WRITER, "favorites", ProfileQuery(page=9))["total"] == 4
    assert repo.list_for_user(WRITER, "favorites", ProfileQuery(page=9))["items"] == []
    assert repo.list_for_user(WRITER, "favorites", ProfileQuery(q="alpha%_"))["total"] == 4
    assert repo.list_for_user(WRITER, "favorites", ProfileQuery(kind="tool"))["total"] == 1
    assert repo.list_for_user(WRITER, "favorites", ProfileQuery(kind="knowledge"))["total"] == 0
    admin = Actor("reviewer", "管理员", "admin", "")
    assert repo.list_for_user(admin, "favorites", ProfileQuery())["total"] == 1
    subscriptions = repo.list_for_user(WRITER, "subscriptions", ProfileQuery(q="独立简介"))
    assert subscriptions["total"] == 1
    assert subscriptions["items"][0]["href"] == f"/applications?resourceId={records['application']}"


def test_scope_and_disabled_resource_filter_before_pagination(
    personal_resources, foundation, connection
):
    repo, records, ids = personal_resources
    rid = records["application"]
    policy = insert(
        connection,
        "resource_visibility_policies",
        resource_type="application",
        resource_id=rid,
        scope="members",
    )
    insert(connection, "resource_visibility_targets", policy_id=policy, user_id=ids["reviewer"])
    query = ProfileQuery(kind="application")
    assert repo.list_for_user(WRITER, "favorites", query)["total"] == 0
    assert repo.list_for_user(WRITER, "subscriptions", query)["total"] == 0
    manager = Actor("writer", "单位管理员", "org_admin", "org-a")
    assert repo.list_for_user(manager, "favorites", query)["total"] == 1
    for revision, (scope, targets, expected) in enumerate(
        [
            ("organizations", ["org-other"], 0),
            ("organizations", ["org-a"], 1),
            ("projects", ["project-other"], 0),
            ("projects", ["project-a"], 1),
        ],
        start=2,
    ):
        update(
            connection,
            "resource_visibility_policies",
            policy,
            scope=scope,
            directory_targets=targets,
            revision=revision,
        )
        assert repo.list_for_user(WRITER, "favorites", query)["total"] == expected
    update(connection, "applications", rid, status="disabled")
    assert (
        repo.list_for_user(Actor("writer", "管理员", "admin", ""), "favorites", query)["total"] == 0
    )
    assert repo.visible_application_ids(WRITER, {str(rid), "old-invalid-id"}) == set()


def test_cancelling_is_idempotent_and_preserves_other_relations(
    personal_resources, foundation, connection
):
    repo, records, ids = personal_resources
    rid = records["application"]
    before = (
        connection.execute(
            sa.select(foundation.table("applications")).where(
                foundation.table("applications").c.id == rid
            )
        )
        .mappings()
        .one()
    )
    app = FastAPI()
    app.state.foundation = foundation
    app.state.application_identity = SimpleNamespace(resolve=lambda request, audience: WRITER)
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as client:
        for _ in range(2):
            assert client.delete(f"/api/v1/profile/favorites/application/{rid}").status_code == 200
        assert client.get("/api/v1/profile/subscriptions").json()["total"] == 1
        assert client.delete(f"/api/v1/profile/subscriptions/{rid}").status_code == 200
    for table_name, expected in [("resource_follows", 0), ("resource_ratings", 1)]:
        table = foundation.table(table_name)
        assert (
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(table)
                .where(table.c.user_id == ids["writer"])
            )
            == expected
        )
    favorites = foundation.table("resource_favorites")
    assert (
        connection.scalar(
            sa.select(sa.func.count())
            .select_from(favorites)
            .where(favorites.c.user_id == ids["reviewer"])
        )
        == 1
    )
    after = (
        connection.execute(
            sa.select(foundation.table("applications")).where(
                foundation.table("applications").c.id == rid
            )
        )
        .mappings()
        .one()
    )
    assert dict(before) == dict(after)


def test_resource_lists_have_bounded_queries(personal_resources, foundation, connection):
    repo, records, ids = personal_resources
    # 增加其他用户的关系不会增加逐条详情查询。
    users = foundation.table("users")
    other_ids = [uuid4() for _ in range(100)]
    connection.execute(
        sa.insert(users),
        [
            {"id": uid, "iam_subject": str(uid), "status": "active", "role": "user"}
            for uid in other_ids
        ],
    )
    relation = foundation.table("resource_favorites")
    connection.execute(
        sa.insert(relation),
        [
            {
                "id": uuid4(),
                "user_id": uid,
                "resource_type": "application",
                "resource_id": records["application"],
            }
            for uid in other_ids
        ],
    )
    statements = []

    def collect(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT"):
            statements.append(statement)

    sa.event.listen(connection, "before_cursor_execute", collect)
    try:
        assert repo.list_for_user(WRITER, "favorites", ProfileQuery())["total"] == 4
        assert len(statements) == 3
        statements.clear()
        assert (
            repo.list_for_user(
                Actor("reviewer", "另一用户", "user", ""), "subscriptions", ProfileQuery()
            )["total"]
            == 0
        )
        assert len(statements) == 2
    finally:
        sa.event.remove(connection, "before_cursor_execute", collect)
