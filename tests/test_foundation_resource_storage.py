"""现有业务 DTO 在新资源表上的持久化验证，全部测试在隔离事务中回滚。"""

from copy import deepcopy
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi import HTTPException

from app.core.config import Settings
from app.db.foundation import RESOURCE_TABLES, FoundationDatabase, utc_now
from app.modules.common.resource_database import ResourceDatabase
from tests.test_resource_foundation import (
    connection,
    flush_constraints,
    pytestmark,
    schema_connection,
)

__all__ = ["connection", "schema_connection", "pytestmark"]


@pytest.fixture
def foundation(connection):
    """使用同一隔离连接装配仓储，不连接 public 业务表。"""
    settings = Settings(_env_file=None)
    database = FoundationDatabase(connection, settings)
    users = database.table("users")
    connection.execute(
        sa.insert(users),
        [
            {
                "id": uuid4(),
                "iam_subject": "writer",
                "display_name": "测试发布者",
                "status": "active",
                "role": "user",
            },
            {
                "id": uuid4(),
                "iam_subject": "reviewer",
                "display_name": "测试管理员",
                "status": "active",
                "role": "admin",
            },
        ],
    )
    return database


def draft(kind: str) -> dict:
    """构造模块发布输入，资源编号由平台生成。"""
    now = utc_now().isoformat()
    key = {"dataset": "datasetId", "application": "applicationId", "tool": "toolId"}.get(
        kind, "resourceId"
    )
    return {
        "id": str(uuid4()),
        key: str(uuid4()),
        "createdBy": "writer",
        "createdByName": "测试发布者",
        "createdAt": now,
        "updatedAt": now,
        "submittedAt": now,
        "status": "pending",
        "revision": 1,
        "data": {
            "name": "测试资源",
            "summary": "已确认新表的业务适配",
            "version": "1.0",
            "owner": "测试单位",
            "category": "办公",
            "tags": [],
            "aiPurpose": "训练",
            "dependencies": [],
        },
        "reviews": [],
        "organizationId": "",
    }


@pytest.mark.parametrize("kind", RESOURCE_TABLES)
def test_publication_review_version_and_interactions(foundation, connection, kind):
    """验证审核和版本原子保存、默认禁用、独立互动及重建仓储后的读取。"""
    repository = ResourceDatabase(foundation, kind, configuration={"revision": 1})
    record = draft(kind)
    with repository.transaction() as session:
        session.put_publication(record)
    flush_constraints(connection)
    with repository.transaction() as session:
        pending = session.get_publication(record["id"])
        assert pending["status"] == "pending"
        assert pending["owner"] == "测试单位"
        assert pending["contact"] == ""
        assert session.list_resources() == []
        pending.update(status="approved", revision=pending["revision"] + 1)
        pending["reviews"].append(
            {
                "id": str(uuid4()),
                "reviewerId": "reviewer",
                "decision": "approved",
                "reason": "符合要求",
                "reviewedAt": utc_now().isoformat(),
                "scores": None,
                "reportFiles": [],
            }
        )
        session.put_publication(pending)
        resource_id = pending[session.resource_key]
    flush_constraints(connection)
    with ResourceDatabase(foundation, kind).transaction() as session:
        resource = session.get_resource(resource_id)
        assert resource["listingStatus"] == "unlisted"
        assert resource["version"] == "1.0"
        resource["listingStatus"] = "listed"
        session.put_resource(resource)
        session.put_entry(
            "interactions",
            f"writer:{resource_id}",
            {
                "actorId": "writer",
                session.resource_key: resource_id,
                "favorite": True,
                "subscribed": True,
                "rating": 4,
                "comment": "资源可用",
            },
        )
    flush_constraints(connection)
    with ResourceDatabase(foundation, kind).transaction(is_readonly=True) as session:
        result = session.get_resource(resource_id)
        assert result["listingStatus"] == "listed"
        assert (result["favoriteCount"], result["rating"], result["ratingCount"]) == (1, 4, 1)
    assert (
        connection.exec_driver_sql(
            f"SELECT count(*) FROM {RESOURCE_TABLES[kind][1]}_versions"
        ).scalar_one()
        == 1
    )
    assert connection.exec_driver_sql("SELECT count(*) FROM resource_reviews").scalar_one() == 1


@pytest.mark.parametrize("kind", RESOURCE_TABLES)
@pytest.mark.parametrize("decision", ["rejected", "needs_changes"])
def test_rejected_resubmission_and_stale_revision(foundation, connection, kind, decision):
    """旧页面不能覆盖已审核申请，退回重提保留上一轮审核历史。"""
    repository = ResourceDatabase(foundation, kind)
    record = draft(kind)
    with repository.transaction() as session:
        session.put_publication(record)
    stale = deepcopy(record)
    with repository.transaction() as session:
        pending = session.get_publication(record["id"])
        pending.update(status=decision, revision=pending["revision"] + 1)
        pending["reviews"].append(
            {
                "id": str(uuid4()),
                "reviewerId": "reviewer",
                "reason": "请补充资料",
                "reportFiles": [],
            }
        )
        session.put_publication(pending)
    flush_constraints(connection)
    with pytest.raises(HTTPException) as error, repository.transaction() as session:
        stale.update(status="draft", revision=stale["revision"] + 1)
        session.put_publication(stale)
    assert error.value.status_code == 409
    with repository.transaction() as session:
        latest = session.get_publication(record["id"])
        assert latest["status"] == decision
        assert latest["reviews"][0]["decision"] == decision
        assert session.list_resources() == []
        latest.update(status="draft", revision=latest["revision"] + 1)
        latest["data"]["summary"] = "修改中的资料"
        session.put_publication(latest)
    flush_constraints(connection)
    with repository.transaction() as session:
        latest = session.get_publication(record["id"])
        assert latest["status"] == decision
        latest.update(
            status="pending", revision=latest["revision"] + 1, submittedAt=utc_now().isoformat()
        )
        latest["data"]["summary"] = "已补充完整资料"
        session.put_publication(latest)
        assert latest["submissionNo"] == 2
    flush_constraints(connection)
    with repository.transaction(is_readonly=True) as session:
        latest = session.get_publication(record["id"])
        assert latest["submissionNo"] == 2
        assert len(latest["reviews"]) == 1
        assert latest["reviews"][0]["decision"] == decision
        assert latest["reviews"][0]["data"]["summary"] != latest["data"]["summary"]
