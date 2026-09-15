"""首页和运营数据库接口；按配置校验登录，仅开放非生产读取，发布保持关闭。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.db.session import SessionDependency
from app.schemas.overview import (
    BudgetInput,
    ComputeAnalysis,
    HomeOverview,
    HomeOverviewResponse,
    OperationsOverviewResponse,
    RankingPeriod,
    Rankings,
    RankingType,
    SnapshotReceipt,
    TokenAnalysis,
    TokenDimension,
    TokenPeriod,
)
from app.services.overview import get_home, get_operations, save_snapshot

# 登录由应用工厂统一校验；未来接入可信发布方后再替换发布依赖。
router = APIRouter(
    prefix="/overview",
    tags=["overview"],
    responses={503: {"description": "接口未开放、数据库不可用或快照损坏"}},
)


def require_overview_write() -> None:
    """发布入口尚未配置可信调用方，所有环境均拒绝写入，不因移除鉴权而匿名放行。"""
    raise HTTPException(503, "overview_publication_not_configured")


write_dependencies = [Depends(require_overview_write)]


async def require_overview_read(request: Request) -> None:
    """只允许服务端开发/测试环境读取；环境标记缺失或生产环境返回 503。"""
    if getattr(request.app.state, "app_env", "production") not in {"development", "test"}:
        raise HTTPException(503, "overview_not_configured")


# 读写共用筛选参数定义，确保别名与可选值一致；默认值在各端点声明。
TokenPeriodQuery = Annotated[TokenPeriod, Query(alias="tokenPeriod", description="Token 统计周期")]
TokenDimensionQuery = Annotated[
    TokenDimension, Query(alias="tokenDimension", description="Token 分析维度")
]
RankingTypeQuery = Annotated[RankingType, Query(alias="rankingType", description="运营排行类型")]
RankingPeriodQuery = Annotated[RankingPeriod, Query(alias="rankingPeriod", description="排行周期")]


@router.get(
    "/home",
    response_model=HomeOverviewResponse,
    summary="获取首页概览",
    dependencies=[Depends(require_overview_read)],
)
def home_overview(request: Request, session: SessionDependency) -> HomeOverviewResponse:
    """返回资源数量、Token、算力、推荐、动态、场景、问题建议和七类首页榜单。"""
    # 仅服务端非生产环境可补充固定样例，生产请求不能通过查询参数启用。
    return get_home(
        session,
        use_demo=getattr(request.app.state, "app_env", "production") in {"development", "test"},
    )


@router.get(
    "/operations",
    response_model=OperationsOverviewResponse,
    summary="获取运营详情",
    dependencies=[Depends(require_overview_read)],
)
def operations_overview(
    request: Request,
    session: SessionDependency,
    token_period: TokenPeriodQuery = "月",
    token_dimension: TokenDimensionQuery = "组织",
    ranking_type: RankingTypeQuery = "用户活跃榜",
    ranking_period: RankingPeriodQuery = "周榜",
) -> OperationsOverviewResponse:
    """筛选值与前端一致；非法值返回 422，非生产缺失板块返回原前端固定样例。"""
    return get_operations(
        session,
        token_period,
        token_dimension,
        ranking_type,
        ranking_period,
        use_demo=getattr(request.app.state, "app_env", "production") in {"development", "test"},
    )


@router.put(
    "/home",
    response_model=SnapshotReceipt,
    dependencies=write_dependencies,
    summary="发布首页完整快照",
)
def update_home(data: HomeOverview, session: SessionDependency) -> SnapshotReceipt:
    """data 为完整首页数据，数组顺序即展示顺序；本阶段计数由可信统计方录入。"""
    return save_snapshot(session, "home", "default", data)


@router.put(
    "/operations/token",
    response_model=SnapshotReceipt,
    dependencies=write_dependencies,
    summary="发布指定周期和维度的 Token 分析",
)
def update_token(
    data: TokenAnalysis,
    session: SessionDependency,
    token_period: TokenPeriodQuery = "月",
    token_dimension: TokenDimensionQuery = "组织",
) -> SnapshotReceipt:
    """只替换选中的周期与维度，其他组合保持原值；不按固定倍率伪造日周统计。"""
    return save_snapshot(session, "token", f"{token_period}:{token_dimension}", data)


@router.put(
    "/operations/compute",
    response_model=SnapshotReceipt,
    dependencies=write_dependencies,
    summary="发布算力监控快照",
)
def update_compute(data: ComputeAnalysis, session: SessionDependency) -> SnapshotReceipt:
    """更新节点负载、汇总及资源池；本阶段由监控适配器或运营方主动推送。"""
    return save_snapshot(session, "compute", "default", data)


@router.put(
    "/operations/budget",
    response_model=SnapshotReceipt,
    dependencies=write_dependencies,
    summary="更新月度预算",
)
def update_budget(data: BudgetInput, session: SessionDependency) -> SnapshotReceipt:
    """只接受已用金额、限额和预警阈值，查询时统一计算预算使用率及提示。"""
    return save_snapshot(session, "budget", "default", data)


@router.put(
    "/operations/rankings",
    response_model=SnapshotReceipt,
    dependencies=write_dependencies,
    summary="发布指定类型和周期的运营排行",
)
def update_rankings(
    data: Rankings,
    session: SessionDependency,
    ranking_type: RankingTypeQuery = "用户活跃榜",
    ranking_period: RankingPeriodQuery = "周榜",
) -> SnapshotReceipt:
    """每行是名称与指标，按请求顺序发布；空数组可清空选中榜单。"""
    return save_snapshot(session, "rankings", f"{ranking_type}:{ranking_period}", data)
