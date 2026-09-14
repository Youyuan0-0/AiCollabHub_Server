"""仅验证新增看板的持久化、筛选与权限边界，不涉及其他业务或 qtest。"""

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.core.auth import Principal
from app.core.config import Settings
from app.main import create_app


class OverviewIdentityProvider:
    """测试专用可信身份，不为运行中的应用增加演示登录或角色请求头。"""

    principal: Principal | None = Principal(
        id="overview-editor",
        display_name="看板维护员",
        permissions=["overview:read", "overview:write"],
    )

    async def authenticate(self, request) -> Principal | None:
        return self.principal

    async def login_url(self, return_to: str) -> str:
        return "https://sso.example.test/login"


@pytest.fixture
def overview_client(tmp_path, monkeypatch):
    """在临时 SQLite 上执行正式迁移，隔离每个用例的数据与身份。"""
    from pathlib import Path

    url = f"sqlite:///{(tmp_path / 'overview.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
    provider = OverviewIdentityProvider()
    # 基础快照用例采用生产口径，避免开发样例掩盖缺失数据及筛选边界。
    settings = Settings(app_env="production", database_url=url, _env_file=None)
    with TestClient(create_app(settings, identity_provider=provider)) as client:
        yield client, provider, settings


def test_home_round_trip_and_restart(overview_client):
    """空态不伪造资源数；发布后所有首页板块保留，重启应用仍可读取。"""
    client, provider, settings = overview_client
    empty = client.get("/api/v1/overview/home")
    assert empty.status_code == 200
    payload = empty.json()
    assert payload.pop("metadata")["missingSections"] == ["home"]
    assert payload["homeStats"][0]["value"] == "—"
    payload["homeStats"][0]["value"] = "12"
    payload["tokenMetrics"] = [{"label": "组织维度", "value": "1M", "description": "本月汇总"}]
    payload["computeMetrics"] = [{"label": "GPU 利用率", "value": 42}]
    payload["featuredResources"] = [
        {"type": "模型", "name": "设计助手", "description": "设计辅助", "path": "/models"}
    ]
    payload["applicationScenarios"] = [
        {"name": "设计优化", "description": "设计辅助", "path": "/applications"}
    ]
    payload["recentUpdates"] = [
        {"time": "2026-09-14", "title": "资源上线", "description": "可申请使用"}
    ]
    payload["questionSuggestions"] = ["如何申请资源？"]
    payload["leaderboardGroups"] = [{"id": "models", "label": "模型", "items": []}]
    assert client.put("/api/v1/overview/home", json=payload).status_code == 200
    # 新应用实例会建立新连接池，确保数据不只存在于内存或某个请求会话中。
    with TestClient(create_app(settings, identity_provider=provider)) as restarted:
        result = restarted.get("/api/v1/overview/home").json()
    meta = result.pop("metadata")
    assert meta["missingSections"] == []
    assert meta["updatedAt"]["home"].endswith("Z")
    assert result == payload
    payload["featuredResources"][0]["path"] = "//outside.example"
    assert client.put("/api/v1/overview/home", json=payload).status_code == 422


def test_operation_filters_and_budget_boundaries(overview_client):
    """四个筛选参数均生效；独立算力/预算不受筛选影响，预算允许超支及零额度。"""
    client, _, _ = overview_client
    root = "/api/v1/overview/operations"
    token = {"tokenSummary": [], "trend": [10, 80], "tokenBreakdown": []}
    assert client.put(f"{root}/token", json=token).status_code == 200
    assert (
        client.put(f"{root}/rankings", json={"rankings": [["设计助手", "12 次"]]}).status_code
        == 200
    )
    compute = {"nodeLoads": ["low"], "computeSummary": [], "computePools": []}
    assert client.put(f"{root}/compute", json=compute).status_code == 200
    assert client.put(f"{root}/budget", json={"used": 120, "limit": 100}).status_code == 200
    data = client.get(root).json()
    assert data["trend"] == [10, 80]
    assert data["rankings"] == [["设计助手", "12 次"]]
    assert data["budget"]["percentage"] == 120
    assert "超出预算" in data["budget"]["message"]
    assert data["metadata"]["missingSections"] == []
    for params, missing, field in [
        ({"tokenPeriod": "日"}, "token", "trend"),
        ({"tokenDimension": "人员"}, "token", "trend"),
        ({"rankingType": "组织贡献榜"}, "rankings", "rankings"),
        ({"rankingPeriod": "总榜"}, "rankings", "rankings"),
    ]:
        filtered = client.get(root, params=params).json()
        assert filtered[field] == []
        assert filtered["metadata"]["missingSections"] == [missing]
        assert filtered["nodeLoads"] == ["low"]
    assert client.put(f"{root}/budget", json={"used": 1, "limit": 0}).status_code == 200
    assert client.get(root).json()["budget"]["message"] == "预算额度为零，已有支出"
    assert client.get(root, params={"tokenPeriod": "年"}).status_code == 422
    assert client.put(f"{root}/token", json={**token, "trend": [101]}).status_code == 422
    assert client.put(f"{root}/rankings", json={"rankings": [["缺少指标"]]}).status_code == 422


def test_overview_permissions(overview_client):
    """生产环境仍要求真实身份与读取权限，客户端环境参数和角色头不能绕过校验。"""
    _, provider, settings = overview_client
    production = settings.model_copy(update={"app_env": "production"})
    with TestClient(create_app(production, identity_provider=provider)) as client:
        provider.principal = None
        for path in ("home", "operations"):
            assert (
                client.get(
                    f"/api/v1/overview/{path}",
                    params={"app_env": "development"},
                    headers={"X-Role": "admin"},
                ).status_code
                == 401
            )
        provider.principal = Principal(
            id="reader", display_name="查看者", permissions=["overview:read"]
        )
        assert client.get("/api/v1/overview/home").status_code == 200
        assert client.get("/api/v1/overview/operations").status_code == 200
        assert (
            client.put(
                "/api/v1/overview/operations/budget",
                json={"used": 0, "limit": 100},
                headers={"X-Role": "admin"},
            ).status_code
            == 403
        )
        provider.principal = Principal(id="visitor", display_name="无权限用户")
        assert client.get("/api/v1/overview/home").status_code == 403
        assert client.get("/api/v1/overview/operations").status_code == 403


@pytest.mark.parametrize("environment", ["development", "test"])
def test_nonproduction_allows_only_anonymous_overview_reads(overview_client, environment):
    """非生产匿名读取原前端样例，既不生成身份，也不开放任何发布端点。"""
    _, _, settings = overview_client
    app = create_app(settings.model_copy(update={"app_env": environment}))
    with TestClient(app) as client:
        home = client.get("/api/v1/overview/home")
        assert home.status_code == 200
        data = home.json()
        assert [item["value"] for item in data["homeStats"]] == ["126", "92", "48"]
        assert len(data["leaderboardGroups"]) == 7
        assert all(len(group["items"]) == 5 for group in data["leaderboardGroups"])
        assert len(data["featuredResources"]) == 4
        assert len(data["applicationScenarios"]) == 4
        assert len(data["recentUpdates"]) == 4
        assert data["metadata"]["demoSections"] == ["home"]
        assert data["metadata"]["missingSections"] == []
        operations = client.get("/api/v1/overview/operations").json()
        assert operations["tokenSummary"][0]["value"] == "45.2M"
        assert operations["budget"]["used"] == 86420
        assert len(operations["computePools"]) == 4
        assert len(operations["nodeLoads"]) == 40
        assert len(operations["rankings"]) == 5
        assert operations["metadata"]["missingSections"] == []
        alternate = client.get(
            "/api/v1/overview/operations", params={"tokenPeriod": "日", "rankingType": "应用热度榜"}
        ).json()
        assert alternate["tokenSummary"][0]["value"] == "3.6M"
        assert alternate["rankings"][0][0] == "知识问答助手"
        assert client.get("/api/v1/auth/session").status_code == 401
        assert client.get("/api/v1/auth/login-url").status_code == 503
        for path in (
            "home",
            "operations/token",
            "operations/compute",
            "operations/budget",
            "operations/rankings",
        ):
            assert client.put(f"/api/v1/overview/{path}", json={}).status_code == 401
        # 环境标记缺失时按生产处理，不能意外扩大匿名访问范围。
        del app.state.app_env
        assert client.get("/api/v1/overview/home").status_code == 401


def test_development_demo_preserves_published_empty_sections(overview_client):
    """开发样例不能覆盖已发布或主动清空的板块，也不能因请求修改而污染后续返回。"""
    _, provider, settings = overview_client
    development = settings.model_copy(update={"app_env": "development"})
    with TestClient(create_app(development, identity_provider=provider)) as client:
        home = client.get("/api/v1/overview/home").json()
        home.pop("metadata")
        home["featuredResources"] = []
        # 请求响应是独立副本，尚未发布时后续查询仍应返回完整样例。
        assert len(client.get("/api/v1/overview/home").json()["featuredResources"]) == 4
        assert client.put("/api/v1/overview/home", json=home).status_code == 200
        published = client.get("/api/v1/overview/home").json()
        assert published["featuredResources"] == []
        assert published["metadata"]["demoSections"] == []
        assert published["metadata"]["updatedAt"]["home"] is not None
        token = {"tokenSummary": [], "trend": [], "tokenBreakdown": []}
        assert client.put("/api/v1/overview/operations/token", json=token).status_code == 200
        operations = client.get("/api/v1/overview/operations").json()
        assert operations["tokenSummary"] == []
        assert operations["metadata"]["demoSections"] == ["compute", "budget", "rankings"]
        assert operations["metadata"]["missingSections"] == []
