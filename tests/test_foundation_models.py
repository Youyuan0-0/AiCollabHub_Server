"""模型版本与场景依赖使用新表的跨模块回归。"""

from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi import HTTPException

from app.modules.applications.publication_schemas import ApplicationDetails
from app.modules.common.database_workspace import DatabaseResourceWorkspace
from app.modules.common.resource_database import ResourceDatabase
from app.modules.common.resource_workspace import ResourceReview
from tests.test_foundation_module_flows import ADMIN, SCORES, WRITER
from tests.test_foundation_resource_storage import draft, foundation
from tests.test_resource_foundation import (
    connection,
    flush_constraints,
    pytestmark,
    schema_connection,
)
from tests.test_resource_requirements import model_data

__all__ = ["foundation", "connection", "schema_connection", "pytestmark"]


def test_model_and_application_version_dependency(foundation, connection):
    """模型数值概览、启用状态和应用引用版本均从数据库保留，跨资源版本不可混用。"""
    models = DatabaseResourceWorkspace(ResourceDatabase(foundation, "model"))
    record = models.save(
        {**model_data(parameters="7B", contextLength="128K"), "owner": "测试单位"},
        WRITER,
        submit=True,
    )
    approved = models.review(
        record["id"],
        ResourceReview(decision="approved", revision=record["revision"], scores=SCORES),
        ADMIN,
        [],
    )
    resource_id = approved["resourceId"]
    model = models.resource(resource_id, ADMIN, True)
    models.set_listing(resource_id, "listed", model["revision"], ADMIN)
    model = models.resource(resource_id, WRITER)
    table = foundation.table("models")
    row = connection.execute(sa.select(table).where(table.c.id == resource_id)).mappings().one()
    assert (row["parameter_count"], row["context_length_tokens"]) == (7_000_000_000, 131_072)
    apps = ResourceDatabase(foundation, "application")
    application = draft("application")
    application["data"]["dependencies"] = [{"kind": "model", "resourceId": resource_id}]
    with apps.transaction() as unit:
        unit.put_publication(application)
    with apps.transaction() as unit:
        loaded = unit.get_publication(application["id"])
        link = loaded["data"]["dependencies"][0]
        assert link["resourceVersionId"] == model["versionId"]
        # 前端带回冻结依赖后，发布表单仍能通过严格契约校验。
        ApplicationDetails(dependencies=[link])
        loaded.update(status="approved", revision=loaded["revision"] + 1)
        loaded["reviews"].append({"id": str(uuid4()), "reviewerId": ADMIN.id, "reportFiles": []})
        unit.put_publication(loaded)
    dependencies = foundation.table("application_dependencies")
    assert connection.scalar(sa.select(sa.func.count()).select_from(dependencies)) == 1
    another = draft("application")
    another["data"]["dependencies"] = [{**link, "resourceVersionId": str(uuid4())}]
    with pytest.raises(HTTPException, match="422"), apps.transaction() as unit:
        unit.put_publication(another)
    flush_constraints(connection)
