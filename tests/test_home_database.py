"""首页使用独立配置及资源表，外部计量维持显式 Mock。"""

import os
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.main import create_app
from app.modules.common.identity import Actor
from app.schemas.overview import ApplicationScenario, BudgetInput, FeaturedResource
from app.services.budget import budget_summary
from app.services.home_activity import HomeActivityStore
from app.services.home_database import (
    automatic_featured_resources,
    home_overview,
    leaderboard_path,
)
from tests.test_foundation_resource_storage import foundation
from tests.test_resource_foundation import connection, flush_constraints, schema_connection

__all__ = ["foundation", "connection", "schema_connection"]


@pytest.mark.parametrize(
    "kind,module,parameter",
    [
        ("dataset", "datasets", "resourceId"),
        ("model", "models", "resourceId"),
        ("application", "applications", "resourceId"),
        ("knowledge", "knowledge", "resourceId"),
        ("tool", "tools", "resource"),
    ],
)
def test_leaderboard_path_targets_resource_detail(kind, module, parameter):
    """混合榜单按真实资源类型打开详情，并对编号做查询参数编码。"""
    assert leaderboard_path({"kind": kind, "id": "item/1"}) == f"/{module}?{parameter}=item%2F1"


def test_automatic_featured_resources_rank_and_limit():
    """自动推荐只取可见目录中的前四条，优先真实互动，同分时选较新资源。"""
    kinds = ("dataset", "model", "tool", "knowledge", "application")
    resources = [
        {
            "id": str(uuid4()),
            "kind": kind,
            "name": f"资源 {index}",
            "summary": "" if index == 2 else f"简介 {index}",
            "favoriteCount": 2 if index < 3 else 0,
            "ratingCount": 1 if index == 1 else 0,
            "downloadCount": 3 if index == 2 else 0,
            "createdAt": f"2026-09-{20 + index:02d}T00:00:00+00:00",
        }
        for index, kind in enumerate(kinds)
    ]
    catalogs = {item["kind"]: {item["id"]: item} for item in resources}
    result = automatic_featured_resources(catalogs)
    assert [item["name"] for item in result] == ["资源 1", "资源 2", "资源 0", "资源 4"]
    assert result[1]["description"] == "资源 2"
    assert result[1]["path"] == f"/tools?resource={resources[2]['id']}"
    assert result[2]["path"] == f"/datasets?resourceId={resources[0]['id']}"
    assert automatic_featured_resources({}) == []


@pytest.mark.parametrize(
    "path",
    [
        "/models",
        "/models?resourceId=00000000-0000-4000-8000-000000000001",
        "/tools?resource=00000000-0000-4000-8000-000000000001",
    ],
)
def test_recommendation_accepts_supported_local_paths(path):
    """首页推荐可进入资源目录或具体详情，工具使用独立查询参数。"""
    assert (
        FeaturedResource(type="模型", name="测试资源", description="简介", path=path).path == path
    )


@pytest.mark.parametrize(
    "path",
    [
        "https://example.com/models",
        "//example.com/models",
        "/models?resourceId=not-a-uuid",
        "/models?resource=00000000-0000-4000-8000-000000000001",
        "/tools?resourceId=00000000-0000-4000-8000-000000000001",
        "/models?resourceId=00000000-0000-4000-8000-000000000001&extra=1",
        "/models#section",
    ],
)
def test_recommendation_rejects_external_or_invalid_paths(path):
    """拒绝外链、错误参数和片段，防止配置把首页卡片导向非预期位置。"""
    with pytest.raises(ValidationError):
        ApplicationScenario(name="测试场景", description="简介", path=path)


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
    activity_store = HomeActivityStore()
    activity_store.create_announcement(
        {
            "title": "测试公告",
            "summary": "公告摘要",
            "contentMarkdown": "公告正文",
            "status": "published",
            "isPinned": False,
        }
    )
    data = home_overview(foundation, actor, activity_store)
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
