"""在专用 PostgreSQL 测试库验证密级经过审核、发布和重新装配仓储后仍一致。"""

import pytest

from app.modules.common.identity import Actor
from app.modules.datasets.admin_schemas import ReviewDataset
from app.modules.datasets.admin_service import DatasetAdminService
from app.modules.datasets.repository import DatasetRepository
from app.modules.datasets.schemas import DatasetInput, SaveDataset
from app.modules.datasets.workspace import DatasetWorkspace
from tests.test_dataset_admin import SCORES
from tests.test_dataset_workspace import data
from tests.test_foundation_resource_storage import foundation
from tests.test_resource_foundation import connection, pytestmark, schema_connection

__all__ = ["foundation", "connection", "schema_connection", "pytestmark"]


@pytest.mark.parametrize("level", ["public", "internal"])
def test_security_level_persists_with_review_snapshot(foundation, level):
    writer = Actor("writer", "测试发布者", "publisher", "")
    admin = Actor("reviewer", "测试管理员", "admin", "")
    workspace = DatasetWorkspace(DatasetRepository(foundation=foundation))
    values = {**data.__wrapped__(), "securityLevel": level}
    created = workspace.save(SaveDataset(data=DatasetInput(**values), submit=True), writer)
    reviewed = DatasetAdminService(workspace).review(
        created["id"],
        ReviewDataset(decision="approved", revision=created["revision"], scores=SCORES),
        admin,
    )
    assert reviewed["syncStatus"] == "local_published"
    reopened = DatasetWorkspace(DatasetRepository(foundation=foundation))
    record = reopened.publication(created["id"], writer)
    assert record["securityLevel"] == record["data"]["securityLevel"] == level
    assert record["reviews"][0]["data"]["securityLevel"] == level
    assert reopened.resource(created["datasetId"], admin)["securityLevel"] == level
