"""首页从正式资源、互动明细及首页配置表生成，不导入固定业务样例。"""

from urllib.parse import urlencode

from sqlalchemy import select

from app.db.foundation import RESOURCE_TABLES, FoundationDatabase
from app.modules.common.identity import Actor, visible
from app.modules.common.resource_database import ResourceDatabase
from app.schemas.overview import (
    BudgetInput,
    ComputeAnalysis,
    HomeOverviewResponse,
    OperationsOverviewResponse,
    Rankings,
    SnapshotMetadata,
    TokenAnalysis,
)
from app.services.budget import budget_summary
from app.services.home_activity import HomeActivityStore, activity_records
from app.services.overview_demo import demo_snapshot

LABELS = {
    "dataset": "数据集",
    "model": "模型",
    "application": "场景应用",
    "knowledge": "知识库",
    "tool": "AI 工具",
}
FEATURED_RESOURCE_LIMIT = 4


def leaderboard_path(item: dict) -> str:
    """按资源类型生成站内详情路径；工具页使用 resource，其余模块使用 resourceId。"""
    kind = item["kind"]
    parameter = "resource" if kind == "tool" else "resourceId"
    return f"/{RESOURCE_TABLES[kind][2]}?{urlencode({parameter: item['id']})}"


def automatic_featured_resources(catalogs: dict[str, dict[str, dict]]) -> list[dict]:
    """从已按上架状态和用户权限过滤的目录中选热门资源，互动同分时优先新资源。"""
    resources = [item for catalog in catalogs.values() for item in catalog.values()]
    ranked = sorted(
        resources,
        key=lambda item: (
            item["favoriteCount"],
            item["ratingCount"],
            item["downloadCount"],
            item["createdAt"],
            item["id"],
        ),
        reverse=True,
    )
    return [
        {
            "type": LABELS[item["kind"]],
            "name": item["name"],
            "description": item["summary"] or item["name"],
            "path": leaderboard_path(item),
        }
        for item in ranked[:FEATURED_RESOURCE_LIMIT]
    ]


def home_overview(
    foundation: FoundationDatabase, actor: Actor, activity_store: HomeActivityStore | None = None
) -> HomeOverviewResponse:
    """展示当前用户可见的资源；动态运营状态来自传入的应用实例内存。"""
    catalogs = {}
    for kind in RESOURCE_TABLES:
        with ResourceDatabase(foundation, kind).transaction(is_readonly=True) as session:
            catalogs[kind] = {
                item["id"]: item
                for item in session.list_resources()
                if item["listingStatus"] == "listed" and visible(actor, item, item["visibility"])
            }
    featured, scenarios = [], []
    with foundation.transaction(is_readonly=True) as session:
        recommendations = foundation.table("home_recommendations")
        # 已存在的人工配置包括禁用项；禁用全部推荐时不再自动补入其他资源。
        has_featured_configuration = (
            session.scalar(
                select(recommendations.c.id)
                .where(recommendations.c.placement == "featured_resource")
                .limit(1)
            )
            is not None
        )
        for row in session.execute(
            select(recommendations)
            .where(recommendations.c.status == "enabled")
            .order_by(recommendations.c.created_at.desc(), recommendations.c.id)
            .limit(200)
        ).mappings():
            kind = row["resource_type"]
            resource = catalogs[kind].get(str(row["resource_id"])) if row["resource_id"] else None
            if row["resource_id"] and resource is None:
                continue
            item = {
                "name": row["title"] or resource["name"],
                "description": row["summary"] or resource["summary"] or resource["name"],
                "path": leaderboard_path(resource) if resource else "/" + RESOURCE_TABLES[kind][2],
            }
            if row["placement"] == "featured_resource":
                featured.append({**item, "type": LABELS[kind]})
            else:
                scenarios.append(item)
        questions = foundation.table("home_question_suggestions")
        suggestions = list(
            session.scalars(
                select(questions.c.question)
                .where(questions.c.status == "enabled")
                .order_by(questions.c.created_at.desc(), questions.c.id)
                .limit(20)
            )
        )
    # 复用首页已按上架和用户范围过滤的目录，公告与运营配置仍读取最新数据库状态。
    updates = activity_records(
        foundation, actor, activity_store or HomeActivityStore(), catalogs=catalogs
    )
    all_resources = [item for catalog in catalogs.values() for item in catalog.values()]
    if not has_featured_configuration:
        featured = automatic_featured_resources(catalogs)

    def entries(items: list[dict]) -> list[dict]:
        """将可见资源转为榜单项，并按模块使用各自的详情定位参数。"""
        return [
            {
                "name": item["name"],
                "tags": item["tags"][:10],
                "metric": f"{item['favoriteCount']} 次收藏",
                "secondary": f"{item['rating']:.1f} 分"
                if item["rating"] is not None
                else "暂无评分",
                "tone": item["kind"]
                if item["kind"] in {"dataset", "model", "application"}
                else "application",
                "path": leaderboard_path(item),
            }
            for item in items[:10]
        ]

    groups = [
        {
            "id": RESOURCE_TABLES[kind][2],
            "label": LABELS[kind] + "榜",
            "items": entries(
                sorted(
                    catalogs[kind].values(), key=lambda item: (-item["favoriteCount"], item["id"])
                )
            ),
        }
        for kind in ("dataset", "model", "application")
    ]
    for key, label, ordered in (
        (
            "rated",
            "评分榜",
            sorted(
                [item for item in all_resources if item["ratingCount"]],
                key=lambda item: (-item["rating"], item["id"]),
            ),
        ),
        (
            "popular",
            "热门榜",
            sorted(all_resources, key=lambda item: (-item["favoriteCount"], item["id"])),
        ),
        (
            "new",
            "最新资源",
            sorted(all_resources, key=lambda item: item["createdAt"], reverse=True),
        ),
    ):
        groups.append({"id": key, "label": label, "items": entries(ordered)})
    groups.append({"id": "users", "label": "用户榜", "items": []})
    return HomeOverviewResponse(
        home_stats=[
            {"label": label, "value": str(len(catalogs[kind]))}
            for kind, label in (
                ("application", "智能体数量"),
                ("dataset", "高质量数据集"),
                ("model", "模型数量"),
            )
        ],
        token_metrics=[],
        compute_metrics=[],
        featured_resources=featured[:100],
        application_scenarios=scenarios[:100],
        recent_updates=updates,
        question_suggestions=suggestions,
        leaderboard_groups=groups,
        metadata=SnapshotMetadata(
            data_source="database",
            updated_at={},
            missing_sections=["token", "compute"],
            demo_sections=[],
        ),
    )


def mock_operations(token_period: str, ranking_type: str) -> OperationsOverviewResponse:
    """外部计量尚未接入时明确返回样例，不访问已经撤回的快照表。"""
    token = demo_snapshot("token", TokenAnalysis, token_period)
    compute = demo_snapshot("compute", ComputeAnalysis, "default")
    budget = demo_snapshot("budget", BudgetInput, "default")
    rankings = demo_snapshot("rankings", Rankings, ranking_type)
    return OperationsOverviewResponse(
        **token.model_dump(),
        **compute.model_dump(),
        **rankings.model_dump(),
        budget=budget_summary(budget, available=True),
        metadata=SnapshotMetadata(
            data_source="mock",
            updated_at={},
            missing_sections=[],
            demo_sections=["token", "compute", "budget", "rankings"],
        ),
    )
