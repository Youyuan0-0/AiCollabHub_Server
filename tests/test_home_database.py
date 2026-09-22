"""首页使用独立配置及资源表，外部计量维持显式 Mock。"""

import os
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.foundation import utc_now
from app.main import create_app
from app.modules.common.identity import Actor
from app.schemas.overview import BudgetInput
from app.services.budget import budget_summary
from app.services.home_database import home_overview
from tests.test_foundation_resource_storage import foundation
from tests.test_resource_foundation import connection, flush_constraints, schema_connection

__all__ = ["foundation", "connection", "schema_connection"]


@pytest.mark.skipif(
    not os.environ.get("RESOURCE_FOUNDATION_TEST_DATABASE_URL"),
    reason="Requires explicit PostgreSQL test connection",
)
def test_home_configuration_and_disabled_resources(foundation, connection):
    """配置按状态展示，空库不返回演示资源，也不把未发布公告暴露给用户。"""
    actor = Actor("writer", "发布者", "publisher", "")
    with foundation.transaction() as session:
        owner = foundation.user_id(session, "reviewer")
        session.execute(
            sa.insert(foundation.table("home_recommendations")).values(
                id=uuid4(),
                placement="application_scenario",
                resource_type="application",
                title="测试场景",
                summary="已启用的场景入口",
                status="enabled",
                creator_id=owner,
            )
        )
        questions = foundation.table("home_question_suggestions")
        session.execute(
            sa.insert(questions),
            [
                {
                    "id": uuid4(),
                    "question": "如何发布资源？",
                    "status": "enabled",
                    "creator_id": owner,
                },
                {
                    "id": uuid4(),
                    "question": "未公开的问题",
                    "status": "disabled",
                    "creator_id": owner,
                },
            ],
        )
        session.execute(
            sa.insert(foundation.table("announcements")).values(
                id=uuid4(),
                title="测试公告",
                summary="公告摘要",
                content_markdown="公告正文",
                status="published",
                published_at=utc_now(),
                creator_id=owner,
            )
        )
    data = home_overview(foundation, actor)
    assert all(item.value == "0" for item in data.home_stats)
    assert data.featured_resources == []
    assert data.application_scenarios[0].name == "测试场景"
    assert data.question_suggestions == ["如何发布资源？"]
    assert data.recent_updates[0].title == "测试公告"
    assert data.metadata.data_source == "database"
    flush_constraints(connection)


@pytest.mark.parametrize("environment", ["development", "test"])
def test_overview_missing_database_and_external_mock(environment):
    """缺库不能伪装资源成功；外部计量明确标注模拟，未开放的写入口保持关闭。"""
    with TestClient(create_app(Settings(app_env=environment))) as client:
        assert client.get("/api/v1/overview/home").status_code == 503
        for path in (
            "home",
            "operations/token",
            "operations/compute",
            "operations/budget",
            "operations/rankings",
        ):
            assert client.put("/api/v1/overview/" + path, json={}).status_code == 503
        operations = client.get("/api/v1/overview/operations")
        # 未配置任何数据库时，基础数据库依赖仍明确返回不可用。
        assert operations.status_code == 503


@pytest.mark.parametrize("used,limit,percentage", [(120, 100, 120), (1, 0, 0), (0, 0, 0)])
def test_budget_boundaries(used, limit, percentage):
    result = budget_summary(BudgetInput(used=used, limit=limit), available=True)
    assert result.percentage == percentage
